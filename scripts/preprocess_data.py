# This file loads an xyz dataset and prepares
# new hdf5 file that is ready for training with on-the-fly dataloading

import logging
import ast
import numpy as np
import json
import random
import tqdm
from glob import glob

import h5py

from joblib import Parallel, delayed


from ase.io import read
import torch
from mace.tools import to_numpy

from mace import tools, data
from mace.data.utils import save_AtomicData_to_HDF5, save_configurations_as_HDF5 #, save_dataset_as_HDF5
from mace.tools.scripts_utils import (get_dataset_from_xyz, 
                                    get_atomic_energies)
from mace.tools import torch_geometric
from mace.modules import compute_avg_num_neighbors, scaling_classes
from mace.modules import scaling_classes

import concurrent.futures
import multiprocessing as mp
import time
import os

results = []

def response(result):
    results.append(result)

def target(receivers):
    # _, receivers = batch.edge_index
    _, counts = torch.unique(receivers, return_counts=True)
    return counts

def neighbor_multi_process(data_loader):
    pool = mp.Pool(processes=len(data_loader))
    for batch in data_loader:
        _, receivers = batch.edge_index
        res=pool.apply_async(target, args=(receivers,), callback=response)

    pool.close()
    pool.join()
    # breakpoint()
    avg_num_neighbors = torch.mean(
    torch.cat(results, dim=0).type(torch.get_default_dtype()))

    return to_numpy(avg_num_neighbors).item()



compute_stats_results = []

def compute_stats_callback(result):
    compute_stats_results.append(result)

def compute_stats_target(file, z_table, scaling, r_max, atomic_energies, batch_size):
    train_dataset = data.HDF5Dataset(file, z_table=z_table, r_max=r_max)
    train_loader = torch_geometric.dataloader.DataLoader(
        dataset=train_dataset, 
        batch_size=batch_size, 
        shuffle=False,
        drop_last=False,
    )
    
    avg_num_neighbors, mean, std = compute_statistics(train_loader, scaling, atomic_energies)
    output = [avg_num_neighbors, mean, std]
    return output

# def pool_compute_stats(path_to_files):
def pool_compute_stats(inputs): #inputs = (path_to_files, z_table, scaling, r_max, atomic_energies, batch_size)
    path_to_files, z_table, scaling, r_max, atomic_energies, batch_size = inputs
    pool = mp.Pool(processes=os.cpu_count())
    
    # loaders = pool.map(compute_stats_target, glob(path_to_files+'/*'))
    # for file in glob(path_to_files+'/*'):
    #     res=pool.apply_async(compute_stats_target, args=(file, z_table, scaling, r_max, atomic_energies, batch_size,), callback=compute_stats_callback)
    re=[pool.apply_async(compute_stats_target, args=(file, z_table, scaling, r_max, atomic_energies, batch_size,)) for file in glob(path_to_files+'/*')]
    
    pool.close()
    pool.join()
    results = [r.get() for r in tqdm.tqdm(re)]
    return np.average(results, axis=0)
    
def compute_statistics(train_loader: torch.utils.data.DataLoader, 
                       scaling: str, 
                       atomic_energies: np.ndarray):
    """
    Compute the average number of neighbors and the mean energy and standard
    deviation of the force components"""
    avg_num_neighbors = compute_avg_num_neighbors(train_loader)
    mean, std = scaling_classes[scaling](train_loader, atomic_energies)
    return avg_num_neighbors, mean, std


def split_array(a: np.ndarray, max_size: int):
    drop_last = False
    if len(a) % 2 == 1:
        a = np.append(a, a[-1])
        drop_last = True
    factors = get_prime_factors(len(a))
    max_factor = 1
    for i in range(1, len(factors) + 1):
        for j in range(0, len(factors) - i + 1):
            if np.prod(factors[j:j + i]) <= max_size:
                test = np.prod(factors[j:j + i])
                if test > max_factor:
                    max_factor = test
    return np.array_split(a, max_factor), drop_last
    
def get_prime_factors(n: int):
    factors = []
    for i in range(2, n + 1):
        while n % i == 0:
            factors.append(i)
            n = n / i 
    return factors


def main():
    start = time.perf_counter()
    """
    This script loads an xyz dataset and prepares
    new hdf5 file that is ready for training with on-the-fly dataloading
    """

    args = tools.build_preprocess_arg_parser().parse_args()

    # Setup
    tools.set_seeds(args.seed)
    random.seed(args.seed)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-8s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[logging.StreamHandler()]
    )

    try:
        config_type_weights = ast.literal_eval(args.config_type_weights)
        assert isinstance(config_type_weights, dict)
    except Exception as e:  # pylint: disable=W0703
        logging.warning(
            f"Config type weights not specified correctly ({e}), using Default"
        )
        config_type_weights = {"Default": 1.0}

    # Data preparation
    collections, atomic_energies_dict = get_dataset_from_xyz(
        train_path=args.train_file,
        valid_path=args.valid_file,
        valid_fraction=args.valid_fraction,
        config_type_weights=config_type_weights,
        test_path=args.test_file, 
        seed=args.seed,
        energy_key=args.energy_key,
        forces_key=args.forces_key,
        stress_key=args.stress_key,
        virials_key=args.virials_key,
        dipole_key=args.dipole_key,
        charges_key=args.charges_key,
    )

    # Atomic number table
    # yapf: disable
    if args.atomic_numbers is None:
        z_table = tools.get_atomic_number_table_from_zs(
            z
            for configs in (collections.train, collections.valid)
            for config in configs
            for z in config.atomic_numbers
        )
    else:
        logging.info("Using atomic numbers from command line argument")
        zs_list = ast.literal_eval(args.atomic_numbers)
        assert isinstance(zs_list, list)
        z_table = tools.get_atomic_number_table_from_zs(zs_list)

    logging.info("Preparing training set")
    if args.shuffle:
        random.shuffle(collections.train)
        
    # with h5py.File(args.h5_prefix + "train.h5", "w") as f:
    #     # split collections.train into batches and save them to hdf5
    #     split_train, drop_last = split_array(collections.train, args.batch_size)
    #     f.attrs["drop_last"] = drop_last
    #     for i, batch in enumerate(tqdm.tqdm(split_train)):
    #         save_configurations_as_HDF5(batch, i, f)

    # split collections.train into batches and save them to hdf5
    split_train = np.array_split(collections.train,os.cpu_count())
    drop_last = False
    if len(collections.train) % 2 == 1:
        drop_last = True
    
    # Define Task for Multiprocessiing
    def multi_train_hdf5(process):
        with h5py.File(args.h5_prefix + "train_" + str(process)+".h5", "w") as f:
            f.attrs["drop_last"] = drop_last
            save_configurations_as_HDF5(split_train[process], process, f)
    
    
# #     #joblib
# #     # p = Parallel(n_jobs=os.cpu_count())(delayed(multi_train_hdf5)(i) for i in tqdm.tqdm(range(os.cpu_count())))
    
    processes = []
    for i in range(os.cpu_count()):
        p = mp.Process(target=multi_train_hdf5, args=[i])
        p.start()
        processes.append(p)
        
    for i in processes:
        i.join()

    logging.info("Computing statistics")
    if len(atomic_energies_dict) == 0:
        atomic_energies_dict = get_atomic_energies(args.E0s, collections.train, z_table)
    atomic_energies: np.ndarray = np.array(
        [atomic_energies_dict[z] for z in z_table.zs]
    )
    logging.info(f"Atomic energies: {atomic_energies.tolist()}")
    _inputs = ['/pscratch/sd/m/mavaylon/processed_chem', z_table, args.scaling, args.r_max, atomic_energies, args.batch_size]
    avg_num_neighbors, mean, std=pool_compute_stats(_inputs)
    # breakpoint()
        
        
        
        # Compute statistics
#         logging.info("Computing statistics")
#         if len(atomic_energies_dict) == 0:
#             atomic_energies_dict = get_atomic_energies(args.E0s, collections.train, z_table)
#         atomic_energies: np.ndarray = np.array(
#             [atomic_energies_dict[z] for z in z_table.zs]
#         )
#         logging.info(f"Atomic energies: {atomic_energies.tolist()}")
#         train_dataset = data.HDF5Dataset(args.h5_prefix + "train.h5", z_table=z_table, r_max=args.r_max)
#         train_loader = torch_geometric.dataloader.DataLoader(
#             dataset=train_dataset, 
#             batch_size=args.batch_size,
#             num_workers=2,
#             shuffle=False,
#             drop_last=False,
#         )
        
#         avg_num_neighbors = neighbor_multi_process(train_loader)
        # breakpoint()
#         # avg_num_neighbors, mean, std = compute_statistics(
#         mean, std = compute_statistics(
#             train_loader, args.scaling, atomic_energies
#         )
    logging.info(f"Average number of neighbors: {avg_num_neighbors}")
    logging.info(f"Mean: {mean}")
    logging.info(f"Standard deviation: {std}")

    # save the statistics as a json
    statistics = {
        "atomic_energies": str(atomic_energies_dict),
        "avg_num_neighbors": avg_num_neighbors,
        "mean": mean,
        "std": std,
        "atomic_numbers": str(z_table.zs),
        "r_max": args.r_max,
    }
    # del train_dataset
    # del train_loader
    with open(args.h5_prefix + "statistics.json", "w") as f:
        json.dump(statistics, f)
    
#     logging.info("Preparing validation set")
#     if args.shuffle:
#         random.shuffle(collections.valid)

#     # with h5py.File(args.h5_prefix + "valid.h5", "w") as f:    
#     #     split_valid, drop_last = split_array(collections.valid, args.batch_size)
#     #     f.attrs["drop_last"] = drop_last
#     #     for i, batch in enumerate(split_valid):
#     #         save_configurations_as_HDF5(batch, i, f)
    
#     split_valid, drop_last = split_array(collections.valid, args.batch_size)

#     def multi_valid_hdf5(process):
#         with h5py.File(args.h5_prefix + "valid_" + str(process)+".h5", "w") as f:
#             f.attrs["drop_last"] = drop_last
#             save_configurations_as_HDF5(split_valid[process], process, f)
    
#     processes = []
#     for i in range(n_batches):
#         p = mp.Process(target=multi_valid_hdf5, args=[i])
#         p.start()
#         processes.append(p)
        
#     for i in processes:
#         i.join()

#     if args.test_file is not None:
#         logging.info("Preparing test sets")
#         for name, subset in collections.tests:
#             with h5py.File(args.h5_prefix + name + "_test.h5", "w") as f:
#                 split_test, drop_last = split_array(subset, args.batch_size)
#                 f.attrs["drop_last"] = drop_last
#                 for i, batch in enumerate(split_test):
#                     save_configurations_as_HDF5(batch, i, f)
    finish = time.perf_counter()
    print(f'Finished in {round(finish-start, 2)} second(s)')

if __name__ == "__main__":
    main()
    
