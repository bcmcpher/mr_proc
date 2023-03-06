import pandas as pd
import numpy as np
import glob
import os
from pathlib import Path
import argparse
import json
import logging
import subprocess
from workflow.dicom_org.utils import search_dicoms, copy_dicoms
from joblib import Parallel, delayed
import workflow.catalog as catalog

#Author: nikhil153
#Date: 07-Oct-2022

fname = __file__
CWD = os.path.dirname(os.path.abspath(fname))

def get_logger(log_file, mode="w", level="DEBUG"):
    """ Initiate a new logger if not provided
    """
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    logger = logging.getLogger(__name__)

    logger.setLevel(level)

    file_handler = logging.FileHandler(log_file, mode=mode)
    formatter = logging.Formatter(log_format)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # output to terminal as well
    stream = logging.StreamHandler()
    streamformat = logging.Formatter(log_format)
    stream.setFormatter(streamformat)
    logger.addHandler(stream)
    
    return logger


def find_mri(participant_ids):
    CMD = ["find_mri", "-claim", "-noconfir"] + participant_ids
    proc = subprocess.run(CMD,capture_output=True,text=True)
    proc_out = proc.stdout.strip().split('\n')

    dcm_download_df = pd.DataFrame(columns=["dicom_file","session_id"])
    print("-"*50)
    print(proc_out)
    print("-"*50)
    i = 0 
    for l in proc_out:
        result = l.find('found')
        if result != -1:
            dcm_file = l.strip().rsplit(" ",1)[1]
            minc_file = dcm_file.lower().find("minc")
            if minc_file == -1:
                mri_index = dcm_file.lower().find("mri")
                if mri_index != -1:
                    visit_id = int(dcm_file[mri_index:].split("_",1)[0][3:])
                else:
                    visit_id = "unknown"

                dcm_download_df.loc[i] = [dcm_file, visit_id]
                i = i + 1

    return dcm_download_df

def filter_duplicate_dicoms(dicom_dir_matches,logger):
    """
    """
    n_dcm_list = []
    for dicom_dir in dicom_dir_matches:
        n_dcm = len(os.listdir(dicom_dir))
        n_dcm_list.append(n_dcm)

    n_max_dcm = np.max(n_dcm_list)
    logger.info(f"Selecting dicom dir with {n_max_dcm} dcm files")
    max_n_dicom_dir = dicom_dir_matches[np.argmax(n_dcm_list)]
    
    return max_n_dicom_dir


def run(global_configs, session_id, n_jobs):
    """ Runs the dicom fetch 
    """
    
    session = f"ses-{session_id}"

    # populate relative paths
    DATASET_ROOT = global_configs["DATASET_ROOT"]
    raw_dicom_dir = f"{DATASET_ROOT}/scratch/raw_dicom/{session}/"
    log_dir = f"{DATASET_ROOT}/scratch/logs/"
    log_file = f"{log_dir}/dicom_fetch.log"

    mr_proc_manifest = f"{DATASET_ROOT}/tabular/demographics/mr_proc_manifest.csv"
    
    logger = get_logger(log_file)
    logger.info("-"*50)
    logger.info(f"Using DATASET_ROOT: {DATASET_ROOT}")
    logger.info(f"session: {session}")
    logger.info(f"Number of parallel jobs: {n_jobs}")

    download_df = catalog.get_new_downloads(mr_proc_manifest, raw_dicom_dir, session_id, logger)
    download_participants = download_df["participant_id"].values
    n_download_participants = len(download_participants)
    logger.info(f"Found {n_download_participants} new participants for download")

    if n_download_participants > 0:
        #####################################################
        dcm_download_df = [] #find_mri(download_participants)
        n_download_success = len(dcm_download_df)

        if n_download_success > 0:
            new_dicom_downloads = []
            for participant_id in download_participants:                        
                # Check for files
                dicom_dir_matches = glob.glob(f"{raw_dicom_dir}/{session_id}/{participant_id}*")

                if len(dicom_dir_matches)== 0:
                    logger.warning(f"No dicom dir match found for {participant_id}")
                    new_dicom_downloads.append(None)
                else:
                    if len(dicom_dir_matches) > 1:
                        logger.info(f"Found multiple ({len(dicom_dir_matches)}) dicom dirs for {participant_id}")
                        link_dicom_dir = filter_duplicate_dicoms(dicom_dir_matches,logger)
                    else:
                        link_dicom_dir = dicom_dir_matches[0]
            
                new_dicom_downloads.append(os.path.basename(link_dicom_dir))

            n_new_dicom_downloads = len(new_dicom_downloads)

            # Add newly processed bids_id to the manifest csv
            manifest_df = pd.read_csv(mr_proc_manifest)
            manifest_df.loc[(manifest_df["participant_id"].astype(str).isin(download_participants))
                            & (manifest_df["session_id"].astype(str) == str(session_id)), 
                            "dicom_file"] = new_dicom_downloads["dicom_file"]

            logger.info(f"Downloaded DICOMs (n={n_new_dicom_downloads}) are now copied into {raw_dicom_dir} and ready for dicom_reorg!")
        else:
            logger.error("No DICOMs were found using find_mri script")
        
            
    else:
        logger.info(f"No new participants found for dicom fetch...")
    

if __name__ == '__main__':
    # argparse
    HELPTEXT = """
    Script to copy dicom dumps into mr_proc scratch/raw_dicom dir
    """
    parser = argparse.ArgumentParser(description=HELPTEXT)
    parser.add_argument('--global_config', type=str, help='path to global config file for your mr_proc dataset')
    parser.add_argument('--session_id', type=str, default=None, help='session (i.e. visit to process)')
    parser.add_argument('--n_jobs', type=int, default=4, help='number of parallel processes')
    args = parser.parse_args()

    # read global configs
    global_config_file = args.global_config
    with open(global_config_file, 'r') as f:
        global_configs = json.load(f)

    session_id = args.session_id
    n_jobs = args.n_jobs

    run(global_configs, session_id, n_jobs)