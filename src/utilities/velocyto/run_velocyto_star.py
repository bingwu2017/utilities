#!/usr/bin/env python
import argparse
import datetime
import os
import re
import subprocess
import time

import utilities.log_util as ut_log
import utilities.s3_util as s3u

import boto3
from boto3.s3.transfer import TransferConfig


CURR_MIN_VER = datetime.datetime(2018, 10, 1, tzinfo=datetime.timezone.utc)


def get_default_requirements():
    return argparse.Namespace(vcpus=2, memory=64000, storage=500, ecr_image="velocyto")


def get_parser():
    """ Construct and return the ArgumentParser object that parses input command.
    """

    parser = argparse.ArgumentParser(
        prog="velocyto.py",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Run expression dynamics (RNA velocity) analysis on smartseq2 data aligned with STAR for a sample folder",
    )

    # required arguments
    requiredNamed = parser.add_argument_group("required arguments")

    requiredNamed.add_argument(
        "--taxon",
        required=True,
        choices=("homo", "mus"),
        help="Reference genome for the velocyto run on smartseq2 data aligned with STAR",
    )

    requiredNamed.add_argument(
        "--s3_input_path",
        required=True,
        help="Location of input sample folder with STAR alignment results",
    )

    requiredNamed.add_argument(
        "--s3_output_path", required=True, help="Location for output",
    )

    # optional arguments
    parser.add_argument(
        "--plates", nargs="*", default=(), help="List of plates to run",
    )

    parser.add_argument(
        "--force_redo",
        action="store_true",
        help="Process files even when results already exist",
    )

    return parser


def run_sample(
    sample_key,
    mask_path,
    gtf_path,
    s3_input_bucket,
    s3_output_bucket,
    s3_output_prefix,
    run_dir,
    logger,
):
    """ Run RNA velocity analysis with Velocyto on smartseq2 data aligned with STAR.

        sample_key - Sample alignment result file name that ends with ".bam"
        mask_path - .gtf file containing intervals to mask (i.e. genes not considered for RNA velocity analysis)
        gtf_path - Path to the .gtf file used for velocyto run
        s3_input_bucket - Name of the bucket with smartseq2 alignment results
        s3_output_bucket - Name of the bucket to store smartseq2 velocyto results
        s3_output_prefix - Pathname under the output bucket to store smartseq2 velocyto results
        run_dir - Path local to the machine on EC2 under which alignment results
                  are stored before uploaded to S3
        logger - Logger object that exposes the interface the code directly uses
    """

    t_config = TransferConfig(num_download_attempts=25)

    sample_name = os.path.basename(sample_key)
    sample_id = sample_name.split(".")[0]  # this is brittle!
    local_sample = os.path.join(run_dir, "input", sample_name)

    s3c.download_file(
        Bucket=s3_input_bucket, Key=sample_key, Filename=local_sample, Config=t_config
    )

    veloctyo_command = [
        "velocyto",
        "run-smartseq2",
        "-o",
        run_dir,
        "-m",
        mask_path,
        "-e",
        sample_id,
        local_sample,
        gtf_path,
    ]

    if ut_log.log_command(
        logger,
        veloctyo_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=True,
    ):
        logger.info(f"velocyto failed on {sample_id}")
        os.remove(local_sample)
        return

    output_file = os.path.join(run_dir, f"{sample_id}.loom")

    logger.info("Uploading {}".format(output_file))
    time.sleep(10)
    s3c.upload_file(
        Filename=output_file,
        Bucket=s3_output_bucket,
        Key=os.path.join(s3_output_prefix, f"{sample_id}.loom"),
        Config=t_config,
    )

    os.remove(local_sample)
    os.remove(output_file)


def main(logger):
    """ Download reference genome, run velocyto jobs, and upload results to S3.

        logger - Logger object that exposes the interface the code directly uses
    """

    parser = get_parser()

    args = parser.parse_args()

    if os.environ.get("AWS_BATCH_JOB_ID"):
        root_dir = os.path.join("/mnt", os.environ["AWS_BATCH_JOB_ID"])
    else:
        root_dir = "/mnt"

    run_dir = os.path.join(root_dir, "data")
    os.makedirs(run_dir)

    os.mkdir(os.path.join(run_dir, "reference"))
    os.mkdir(os.path.join(run_dir, "input"))

    if args.taxon == "homo":
        gtf_file = "HG38-PLUS.gtf"
        mask_file = "hg38_rmsk.gtf"
    elif args.taxon == "mus":
        gtf_file = "MM10-PLUS.gtf"
        mask_file = "mm10_rmsk.gtf"
    else:
        raise ValueError("Invalid taxon {}".format(args.taxon))

    s3_input_bucket, s3_input_prefix = s3u.s3_bucket_and_key(args.s3_input_path)

    logger.info(
        f"""Run Info: gtf_file:\t{gtf_file}
                    mask_file:\t{mask_file}
                        taxon:\t{args.taxon}
                s3_input_path:\t{args.s3_input_path}"""
    )

    gtf_path = os.path.join(run_dir, "reference", gtf_file)
    mask_path = os.path.join(run_dir, "reference", mask_file)
    s3u.download_files(
        [f"velocyto/{gtf_file}", f"velocyto/{mask_file}"],
        [gtf_path, mask_path],
        b="czbiohub-reference",
        n_proc=2,
    )

    sample_re = re.compile(f"([^/]+).{args.taxon}.Aligned.out.sorted.bam$")
    plate_set = set(args.plates)

    s3_output_bucket, s3_output_prefix = s3u.s3_bucket_and_key(args.s3_output_path)

    # Check the output folder for existing runs
    if not args.force_redo:
        output = s3u.prefix_gen(
            s3_output_bucket, s3_output_prefix, lambda r: (r["LastModified"], r["Key"]),
        )
    else:
        output = []

    output_files = {
        os.path.basename(fn).split(".")[0]
        for dt, fn in output
        if fn.endswith(".loom") and dt > CURR_MIN_VER
    }

    sample_file = [
        fn
        for fn in s3u.get_files(s3_input_bucket, s3_input_prefix)
        if fn.endswith(f"{args.taxon}.Aligned.out.sorted.bam")
    ]
    assert (
        len(sample_file) == 1
    ), "There should be only one .bam file for the input sample folder"

    plate_sample = ""

    matched = sample_re.search(os.path.basename(sample_file[0]))
    if matched.group(1) not in output_files:
        if len(plate_set) == 0 or matched.group(1).split("_")[1] in plate_set:
            plate_sample = sample_file[0]

    run_sample(
        plate_sample,
        mask_path,
        gtf_path,
        s3_input_bucket,
        s3_output_bucket,
        s3_output_prefix,
        run_dir,
        logger,
    )
    time.sleep(30)

    logger.info("Job completed")


if __name__ == "__main__":
    mainlogger, log_file, file_handler = ut_log.get_logger(__name__)
    s3c = boto3.client("s3")
    main(mainlogger)
