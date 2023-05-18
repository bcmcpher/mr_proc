import argparse
import os
import shutil
from pathlib import Path
import json
import subprocess

import numpy as np
import nibabel as nib
from bids import BIDSLayout

import workflow.logger as my_logger

#Author: bcmcpher
#Date: 14-Apr-2023 (last update)
fname = __file__
CWD = os.path.dirname(os.path.abspath(fname))

# env vars relative to the container.

MEM_MB = 4000


def parse_data(bids_dir, participant_id, session_id, logger=None):
    """ Parse and verify the input files to build TractoFlow's simplified input to avoid their custom BIDS filter
    """

    ## because why parse subject ID the same as bids ID?
    subj = participant_id.replace('sub-', '')

    logger.info('Building BIDS Layout...')
    
    ## parse directory
    layout = BIDSLayout(bids_dir)

    ## pull every t1w / dwi file name from BIDS layout
    anat_files = layout.get(subject=subj, session=session_id, suffix='T1w', extension='.nii.gz', return_type='object')
    dmri_files = layout.get(subject=subj, session=session_id, suffix='dwi', extension='.nii.gz', return_type='object')
        
    ## preallocate candidate anatomical files
    canat = []

    logger.info("Parsing Anatomical Files...")
    for idx, anat in enumerate(anat_files):

        ## pull the data
        tmeta = anat.get_metadata()
        tvol = anat.get_image()

        ## because PPMI doesn't have full sidecars
        
        try:
            tmcmode = tmeta['MatrixCoilMode']
        except:
            tmcmode = 'unknown'

        try:
            torient = tmeta['ImageOrientationText']
        except:
            torient = 'sag'

        try:
            tprotocol = tmeta['ProtocolName']
        except:
            tprotocol = 'unknown'
            
        logger.info("- "*25)
        logger.info(anat.filename)
        logger.info(f"Scan Type: {tmcmode}")
        logger.info(f"Data Shape: {tvol.shape}")

        ## if sense is in the encoded header drop it
        if tmcmode.lower() == 'sense':
            continue

        ## if it's not a sagittal T1, it's probably not the main
        if not torient.lower() == 'sag':
            continue

        ## look for Neuromelanin type scan in name somewhere
        if ('neuromel' in tprotocol.lower()):
            continue
        
        ## heudiconv heuristics file has some fields that could be reused.
        ## how much effort are we supposed to spend generalizing parsing to other inputs?
        
        ## append the data if it passes all the skips
        canat.append(anat)

    logger.info("- "*25)

    ## error if nothing passes
    if len(canat) == 0:
        raise ValueError(f'No valid T1 anat file for {participant_id} in ses-{session_id}.')
        
    ## check how many candidates there are
    if len(canat) > 1:
        logger.info('Still have to pick one...')
        npart = [ len(x.get_entities()) for x in canat ]
        oanat = canat[np.argmin(npart)]
    else:
        oanat = canat[0]

    logger.info(f"Selected anat file: {oanat.filename}")
    logger.info("= "*25)
    
    ## preallocate candidate dmri inputs
    cdmri = []
    cbv = np.empty(len(dmri_files))
    cnv = np.empty(len(dmri_files))
    cpe = []
    
    logger.info("Parsing Diffusion Files...")
    for idx, dmri in enumerate(dmri_files):

        tmeta = dmri.get_metadata()
        tvol = dmri.get_image()

        try:
            tpedir = tmeta['PhaseEncodingDirection']
        except:
            raise ValueError("INCOMPLETE SIDECAR: ['PhaseEncodingDirection'] is not defined in sidecar. This is required to accurately parse the dMRI data.")
            
        logger.info("- "*25)
        logger.info(dmri.filename)
        logger.info(f"Encoding Direction: {tmeta['PhaseEncodingDirection']}")
        logger.info(f"Data Shape: {tvol.shape}")

        ## store phase encoding data
        cpe.append(tmeta['PhaseEncodingDirection'])
        
        ## store image dimension
        if len(tvol.shape) == 4:
            cnv[idx] = tvol.shape[-1]
        elif len(tvol.shape) == 3:
            cnv[idx] = 1
        else:
            raise ValueError('dMRI File: {dmri.filename} is not 3D/4D.')
            
        ## build paths to bvec / bval data
        tbvec = Path(bids_dir, participant_id, 'ses-' + session_id, 'dwi', dmri.filename.replace('.nii.gz', '.bvec')).joinpath()
        tbval = Path(bids_dir, participant_id, 'ses-' + session_id, 'dwi', dmri.filename.replace('.nii.gz', '.bval')).joinpath()

        ## if bvec / bval data exist
        if os.path.exists(tbvec) & os.path.exists(tbval):
            logger.info('BVEC / BVAL data exists for this file')
            cbv[idx] = 1
        else:
            logger.info('BVEC / BVAL data does not exist for this file')
            cbv[idx] = 0

        ## append to output (?)
        cdmri.append(dmri)
        
    logger.info("- "*25)

    ## if there's more than 1 candidate with bv* files
    if sum(cbv == 1) > 1:
        
        logger.info("Continue checks assuming 2 directed files...")

        dmrifs = []
        
        ## pull the full sequences
        for idx, x in enumerate(cbv):
            if x == 1:
                logger.info(f"File {idx+1}: {dmri_files[idx].filename}")
                dmrifs.append(dmri_files[idx])
        
        ## if there are more than 2, quit - bad input
        if len(dmrifs) > 2:
            raise ValueError('Too many candidate full sequences.')

        ## split out to separate files
        dmrifs1 = dmrifs[0]
        dmrifs2 = dmrifs[1]
        
        ## pull phase encoding direction
        dmrifs1pe = dmrifs1.get_metadata()['PhaseEncodingDirection']
        dmrifs2pe = dmrifs2.get_metadata()['PhaseEncodingDirection']

        ## if the phase encodings are the same axis
        if (dmrifs1pe[0] == dmrifs2pe[0]):

            logger.info(f'Phase encoding axis: {dmrifs1pe[0]}')

            ## if the phase encodings match exactly
            if (dmrifs1pe == dmrifs2pe):

                ## THIS HAPPENS WITH A IDENTICAL RUN-1/RUN-2 IN PPMI
                ## ARE THESE MEANT TO BE TIME 1 / TIME 2?
                ## UNSURE HOW I SHOULD DEAL WITH MULTIPLE RUNS IN dMRI - NOT COMMON
                logger.info('Sequences are not reverse encoded. Ignoring second sequence.')
                didx = dmri_files.index(dmrifs1)
                rpe_out = None

            ## they're the same axis in opposite orientations
            else:

                logger.info(f"Foward Phase Encoding:  {dmrifs1pe}")
                logger.info(f"Reverse Phase Encoding: {dmrifs2pe}")

                ## if the sequences are the same length
                if (dmrifs1nv == dmrifs2nv):
                    
                    logger.info('N volumes match. Assuming mirrored sequences.')

                    ## verify that bvecs match?

                    ## pull the number of volumes
                    dmrifs1nv = dmrifs1.get_image().shape[3]
                    dmrifs2nv = dmrifs1.get_image().shape[3]

                    ## pull the first as forward
                    didx = dmri_files.index(dmrifs1) 

                    ## pull the second as reverse
                    rpeimage = dmrifs2.get_image()

                    ## load image data
                    rpedata = rpeimage.get_fdata() 

                    ## load bval data
                    rpeb0s = np.loadtxt(Path(bids_dir, participant_id, 'ses-' + session_id, 'dwi', dmrifs2.filename.replace('.nii.gz', '.bval')).joinpath())

                    ## create average b0 data from sequence
                    rpeb0 = np.mean(rpedata[:,:,:,rpeb0s == 0], 3)

                    ## write to disk
                    rpe_out = f'/tmp/{participant_id}_rpe_b0.nii.gz'
                    rpe_data = nib.nifti1.Nifti1Image(rpeb0, rpeimage.affine)
                    rpe_shape = rpe_data.shape
                    nib.save(rpe_data, rpe_out)

                else:

                    raise ValueError('The sequences are suffieciently mismatched that an acquisition or conversion error is likley to have occurred.')

        else:

            raise ValueError(f'The phase encodings are on different axes: {dmrifs1pe}, {dmrifs2pe}\nCannot determine what to do.')
            
    else:
        
        logger.info("Continue checks assuming 1 directed file...")

        ## pull the index of the bvec that exists
        didx = np.argmax(cbv)
        
        ## pull phase encoding for directed volume
        fpe = cpe[didx]

        ## clean fpe of unnecessary + if it's there
        if fpe[-1] == "+":
            fpe = fpe[0]
        
        ## determine the reverse phase encoding
        if (len(fpe) == 1):
            rpe = fpe + "-"
        else:
            rpe = fpe[0]

        logger.info(f"Foward Phase Encoding:  {fpe}")
        logger.info(f"Reverse Phase Encoding: {rpe}")
            
        ## look for the reverse phase encoded file in the other candidates
        if (rpe in cpe):
            
            rpeb0 = dmri_files[cpe.index(rpe)]
            rpevol = rpeb0.get_image()
            rpe_out = f'/tmp/{participant_id}_rpe_b0.nii.gz'
            
            ## if the rpe file has multiple volumes
            if len(rpevol.shape) == 4:

                logger.info('An RPE file is present: Averaging b0 volumes to single RPE volume...')
                ## load and average the volumes
                rpedat = rpevol.get_fdata()
                rpedat = np.mean(rpedat, 3)

                ## and write the file to /tmp
                rpe_data = nib.nifti1.Nifti1Image(rpedat, rpevol.affine)
                rpe_shape = rpe_data.shape
                nib.save(rpe_data, rpe_out)
                
            else:

                logger.info('An RPE file is present: Copying single the single RPE b0 volume...')
                ## otherwise, copy the input file to tmp
                rpe_shape = rpevol.shape
                shutil.copyfile(rpeb0, rpe_out)
                            
        else:
            
            logger.info("No RPE is found in candidate files")
            rpe_out = None
            
    logger.info("= "*25)
    
    ## default assignments
    dmrifile = Path(bids_dir, participant_id, 'ses-' + session_id, 'dwi', dmri_files[didx].filename).joinpath()
    bvalfile = Path(bids_dir, participant_id, 'ses-' + session_id, 'dwi', dmri_files[didx].filename.replace('.nii.gz', '.bval')).joinpath()
    bvecfile = Path(bids_dir, participant_id, 'ses-' + session_id, 'dwi', dmri_files[didx].filename.replace('.nii.gz', '.bvec')).joinpath()
    anatfile = Path(bids_dir, participant_id, 'ses-' + session_id, 'anat', oanat.filename).joinpath()

    ## condition empty path
    if rpe_out == None:
        rpe_file = None
    else:
        rpe_file = Path(rpe_out)
        
    ## return the paths to the input files to copy
    return(dmrifile, bvalfile, bvecfile, anatfile, rpe_file)

def run_tractoflow(participant_id, global_configs, session_id, output_dir, use_bids_filter, logger=None):
    """ Runs TractoFlow command with Nextflow
    """

    ## extract the config options
    DATASET_ROOT = global_configs["DATASET_ROOT"]
    CONTAINER_STORE = global_configs["CONTAINER_STORE"]
    TRACTOFLOW_CONTAINER = global_configs["PROC_PIPELINES"]["tractoflow"]["CONTAINER"]
    TRACTOFLOW_VERSION = global_configs["PROC_PIPELINES"]["tractoflow"]["VERSION"]
    TRACTOFLOW_CONTAINER = TRACTOFLOW_CONTAINER.format(TRACTOFLOW_VERSION)
    SINGULARITY_TRACTOFLOW = f"{CONTAINER_STORE}{TRACTOFLOW_CONTAINER}"
    LOGDIR = f"{DATASET_ROOT}/scratch/logs"

    ## initialize the logger
    if logger is None:
        log_file = f"{LOGDIR}/{participant_id}_ses-{session_id}_tractoflow.log"
        logger = my_logger.get_logger(log_file)

    ## log the info
    logger.info("-"*75)
    logger.info(f"Using DATASET_ROOT: {DATASET_ROOT}")
    logger.info(f"Using participant_id: {participant_id}, session_id: {session_id}")

    ## define default output_dir if it's not overwrote
    if output_dir is None:
        output_dir = f"{DATASET_ROOT}/derivatives"

    ## build paths to files
    bids_dir = f"{DATASET_ROOT}/bids"
    tractoflow_dir = f"{output_dir}/tractoflow/v{TRACTOFLOW_VERSION}"

    ## Copy bids_filter.json `<DATASET_ROOT>/bids/bids_filter.json`
    if use_bids_filter:
        logger.info(f"Copying ./bids_filter.json to {DATASET_ROOT}/bids/bids_filter.json (to be seen by Singularity container)")
        shutil.copyfile(f"{CWD}/bids_filter.json", f"{bids_dir}/bids_filter.json")

    ## build paths for outputs
    tractoflow_out_dir = f"{tractoflow_dir}/output/"
    tractoflow_home_dir = f"{tractoflow_out_dir}/{participant_id}"
    if not os.path.exists(Path(f"{tractoflow_home_dir}")):
        Path(f"{tractoflow_home_dir}").mkdir(parents=True, exist_ok=True)

    ## build paths for working inputs
    tractoflow_input_dir = f"{tractoflow_dir}/input"
    tractoflow_subj_dir = f"{tractoflow_input_dir}/{participant_id}"
    tractoflow_work_dir = f"{tractoflow_dir}/work/{participant_id}"

    if not os.path.exists(Path(f"{tractoflow_subj_dir}")):
        Path(f"{tractoflow_subj_dir}").mkdir(parents=True, exist_ok=True)

    if not os.path.exists(Path(f"{tractoflow_work_dir}")):
        Path(f"{tractoflow_work_dir}").mkdir(parents=True, exist_ok=True)

    ## call the file parser to copy the correct files to the input structure
    dmrifile, bvalfile, bvecfile, anatfile, rpe_file = parse_data(bids_dir, participant_id, session_id, logger)
        
    # ## copy the bids data into this folder in their "simple" input structure b/c bids parsing doesn't work
    # ## and uses a unique filter that isn't easy / worth parsing
    # dmrifile = f"{bids_dir}/{participant_id}/ses-{session_id}/dwi/{participant_id}_ses-{session_id}_run-1_dwi.nii.gz"  ## bad path generalization for now.
    # bvalfile = f"{bids_dir}/{participant_id}/ses-{session_id}/dwi/{participant_id}_ses-{session_id}_run-1_dwi.bval"    ## not trivial to parse and build
    # bvecfile = f"{bids_dir}/{participant_id}/ses-{session_id}/dwi/{participant_id}_ses-{session_id}_run-1_dwi.bvec"    ## the right names, esp. if bids
    # anatfile = f"{bids_dir}/{participant_id}/ses-{session_id}/anat/{participant_id}_ses-{session_id}_run-1_T1w.nii.gz" ## parsing isn't more helpful
    # #rpe_file = too hard to parse for now - too many valid names are possible for input.
    # ## will need to extract and average b0s after verifying the rpe is actually reversed by checking sidecar.

    ## just make copies if they aren't already there - resume option cannot work w/ modfied (recopied) files, so check first
    ## delete on success?
    if len(os.listdir(tractoflow_subj_dir)) == 0:
        shutil.copyfile(dmrifile, tractoflow_subj_dir + '/dwi.nii.gz')
        shutil.copyfile(bvalfile, tractoflow_subj_dir + '/bval')
        shutil.copyfile(bvecfile, tractoflow_subj_dir + '/bvec')
        shutil.copyfile(anatfile, tractoflow_subj_dir + '/t1.nii.gz')
        if not rpe_file == None:
            shutil.copyfile(rpe_file, tractoflow_work_dir + '/rev_b0.nii.gz')

    # ## cd to tractoflow_work_dir to control where the "work" folder ends up
    # os.chdir(tractoflow_work_dir)
    # logger.info(f"Setting working directory to: {tractoflow_work_dir}")

    ## drop sub- from participant ID
    tf_id = participant_id.replace('sub-', '')
    
    ## generalize as inputs - eventually
    dti_shells=1000
    fodf_shells=1000
    sh_order=6
    profile='fully_reproducible'
    ncore=4

    ## path to pipelines
    TRACTOFLOW_PIPE=f'{DATASET_ROOT}/workflow/proc_pipe/tractoflow'
    
    ## this is fixed for every run - nextflow is a dependency b/c it's too hard to package in the containers that will call this
    ## this reality prompts the planned migration to micapipe - or anything else, honestly
    NEXTFLOW_CMD=f"nextflow run {TRACTOFLOW_PIPE}/tractoflow/main.nf -with-singularity {SINGULARITY_TRACTOFLOW} -work-dir {tractoflow_work_dir} -with-trace {LOGDIR}/{participant_id}_ses-{session_id}_nf-trace.txt -with-report {LOGDIR}/{participant_id}_ses-{session_id}_nf-report.html"
    
    ## compose tractoflow arguments
    TRACTOFLOW_CMD=f""" --input {tractoflow_input_dir} --output_dir {tractoflow_out_dir} --participant-label "{tf_id}" --dti_shells "0 {dti_shells}" --fodf_shells "0 {fodf_shells}" --sh_order {sh_order} --profile {profile} --processes {ncore}"""

    ## I have no idea why the inputs have to be parsed this way. The TractoFlow arguments can be printed multiple ways that appear consistent with the documentation but are parsed incorrectly by nextflow.
    ## .nexflow.log (a run log that documents what is getting parsed by nexflow) shows additional quotes being added around the dti / fodf parameters. Something like: "'0' '1000'"
    ## Obviously, this breaks the calls that nextflow tries to make at somepoint (typically around Normalize_DWI) because half the command becomes an unfinished text block from the mysteriously added quotes.
    ## I don't know if the problem is python printing unhelpful/inaccurate text to the user or if nextflow can't parse its own input arguments correctly.
    
    ## add resume option if working directory is not empty
    if not len(os.listdir(tractoflow_work_dir)) == 0:
        TRACTOFLOW_CMD = TRACTOFLOW_CMD + " -resume"
    
    ## build command line call
    CMD_ARGS = NEXTFLOW_CMD + TRACTOFLOW_CMD 
    CMD=CMD_ARGS
    #CMD = CMD_ARGS.split()

    ## log what is called
    logger.info(f"Running TractoFlow...")
    logger.info("-"*50)
    logger.info(f"CMD:\n{CMD_ARGS}")
    logger.info("-"*50)
    logger.info(f"Calling TractoFlow for participant: {participant_id}")

    ## there's probably a better way to try / catch the .run() call here
    try:
        tractoflow_proc = subprocess.run(CMD, shell=True)
        logger.info("-"*75)
    except Exception as e:
        logger.error(f"TractoFlow run failed with exceptions: {e}")
        logger.info("-"*75)


if __name__ == '__main__':
    ## argparse
    HELPTEXT = """
    Script to run TractoFlow 
    """

    ## parse intputs
    parser = argparse.ArgumentParser(description=HELPTEXT)
    parser.add_argument('--global_config', type=str, help='path to global configs for a given mr_proc dataset', required=True)
    parser.add_argument('--participant_id', type=str, help='participant id', required=True)
    parser.add_argument('--session_id', type=str, help='session id for the participant', required=True)
    parser.add_argument('--output_dir', type=str, default=None, help='specify custom output dir (if None --> <DATASET_ROOT>/derivatives)')
    parser.add_argument('--use_bids_filter', action='store_true', help='use bids filter or not')

    ## extract arguments
    args = parser.parse_args()
    global_config_file = args.global_config
    participant_id = args.participant_id
    session_id = args.session_id
    output_dir = args.output_dir # Needed on BIC (QPN) due to weird permissions issues with mkdir
    use_bids_filter = args.use_bids_filter

    ## Read global config
    with open(global_config_file, 'r') as f:
        global_configs = json.load(f)

    ## make valid tractoflow call based on inputs    
    run_tractoflow(participant_id, global_configs, session_id, output_dir, use_bids_filter)