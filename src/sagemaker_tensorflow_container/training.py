# Copyright 2018-2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the 'License'). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the 'license' file accompanying this file. This file is
# distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
from __future__ import absolute_import

import json
import logging
import multiprocessing
import os
import subprocess
import time

from sagemaker_training import entry_point, environment, mapping, runner
import tensorflow as tf

from sagemaker_tensorflow_container import s3_utils

logger = logging.getLogger(__name__)

SAGEMAKER_PARAMETER_SERVER_ENABLED = "sagemaker_parameter_server_enabled"
SAGEMAKER_DISTRIBUTED_DATAPARALLEL_ENABLED = "sagemaker_distributed_dataparallel_enabled"
SAGEMAKER_MULTI_WORKER_MIRRORED_STRATEGY_ENABLED = (
    "sagemaker_multi_worker_mirrored_strategy_enabled"
)
MODEL_DIR = "/opt/ml/model"


def _is_host_master(hosts, current_host):
    return current_host == hosts[0]


def _build_tf_config_for_ps(hosts, current_host, ps_task=False):
    """Builds a dictionary containing cluster information based on number of hosts and number of
    parameter servers.

    Args:
        hosts (list[str]): List of host names in the cluster
        current_host (str): Current host name
        ps_task (bool): Set to True if this config is built for a parameter server process
            (default: False)

    Returns:
        dict[str: dict]: A dictionary describing the cluster setup for distributed training.
            For more information regarding TF_CONFIG:
            https://cloud.google.com/ml-engine/docs/tensorflow/distributed-training-details
    """
    # Assign the first host as the master. Rest of the hosts if any will be worker hosts.
    # The first ps_num hosts will also have a parameter task assign to them.
    masters = hosts[:1]
    workers = hosts[1:]
    ps = hosts if len(hosts) > 1 else None

    def host_addresses(hosts, port=2222):
        return ["{}:{}".format(host, port) for host in hosts]

    tf_config = {"cluster": {"master": host_addresses(masters)}, "environment": "cloud"}

    if ps:
        tf_config["cluster"]["ps"] = host_addresses(ps, port="2223")

    if workers:
        tf_config["cluster"]["worker"] = host_addresses(workers)

    if ps_task:
        if ps is None:
            raise ValueError(
                "Cannot have a ps task if there are no parameter servers in the cluster"
            )
        task_type = "ps"
        task_index = ps.index(current_host)
    elif _is_host_master(hosts, current_host):
        task_type = "master"
        task_index = 0
    else:
        task_type = "worker"
        task_index = workers.index(current_host)

    tf_config["task"] = {"index": task_index, "type": task_type}
    return tf_config


def _build_tf_config_for_mwms(hosts, current_host):
    """Builds a dictionary containing cluster information based on number of workers
    for Multi Worker Mirrored distribution strategy.

    Args:
        hosts (list[str]): List of host names in the cluster
        current_host (str): Current host name

    Returns:
        dict[str: dict]: A dictionary describing the cluster setup for distributed training.
            For more information regarding TF_CONFIG:
            https://cloud.google.com/ml-engine/docs/tensorflow/distributed-training-details
    """
    workers = hosts

    def host_addresses(hosts, port=8890):
        return ["{}:{}".format(host, port) for host in hosts]

    tf_config = {"cluster": {}, "environment": "cloud"}
    tf_config["cluster"]["worker"] = host_addresses(workers)
    tf_config["task"] = {"index": workers.index(current_host), "type": "worker"}

    return tf_config


def _run_ps(env, cluster):
    logger.info("Running distributed training job with parameter servers")

    cluster_spec = tf.train.ClusterSpec(cluster)
    task_index = env.hosts.index(env.current_host)
    # Force parameter server to run on cpu. Running multiple TensorFlow processes on the same
    # GPU is not safe:
    # https://stackoverflow.com/questions/46145100/is-it-unsafe-to-run-multiple-tensorflow-processes-on-the-same-gpu
    no_gpu_config = tf.compat.v1.ConfigProto(device_count={"GPU": 0})

    server = tf.distribute.Server(
        cluster_spec, job_name="ps", task_index=task_index, config=no_gpu_config
    )

    multiprocessing.Process(target=lambda: server.join()).start()


def _run_worker(env, cmd_args, tf_config):
    env_vars = env.to_env_vars()
    env_vars["TF_CONFIG"] = json.dumps(tf_config)

    entry_point.run(
        uri=env.module_dir,
        user_entry_point=env.user_entry_point,
        args=cmd_args,
        env_vars=env_vars,
        capture_error=True,
    )


def _wait_until_master_is_down(master):
    while True:
        try:
            subprocess.check_call(
                ["curl", "{}:2222".format(master)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            logger.info("master {} is still up, waiting for it to exit".format(master))
            time.sleep(10)
        except subprocess.CalledProcessError:
            logger.info("master {} is down, stopping parameter server".format(master))
            return


def train(env, cmd_args):
    """Get training job environment from env and run the training job.

    Args:
        env (sagemaker_training.environment.Environment): Instance of Environment class
    """
    parameter_server_enabled = (
        env.additional_framework_parameters.get(SAGEMAKER_PARAMETER_SERVER_ENABLED, False)
        and len(env.hosts) > 1
    )
    multi_worker_mirrored_strategy_enabled = env.additional_framework_parameters.get(
        SAGEMAKER_MULTI_WORKER_MIRRORED_STRATEGY_ENABLED, False
    )
    sagemaker_distributed_dataparallel_enabled = env.additional_framework_parameters.get(
        SAGEMAKER_DISTRIBUTED_DATAPARALLEL_ENABLED, False
    )

    env_vars = env.to_env_vars()

    # Setup
    if env.current_instance_group in env.distribution_instance_groups:
        if parameter_server_enabled:

            tf_config = _build_tf_config_for_ps(hosts=env.distribution_hosts, current_host=env.current_host)
            logger.info("Running distributed training job with parameter servers")

        elif multi_worker_mirrored_strategy_enabled:

            env_vars["TF_CONFIG"] = json.dumps(
                _build_tf_config_for_mwms(hosts=env.distribution_hosts, current_host=env.current_host)
            )
            logger.info("Running distributed training job with multi_worker_mirrored_strategy setup")

    runner_type = runner.ProcessRunnerType

    # Run
    if parameter_server_enabled:

        logger.info("Launching parameter server process")
        _run_ps(env, tf_config["cluster"])
        logger.info("Launching worker process")
        _run_worker(env, cmd_args, tf_config)

        if not _is_host_master(env.hosts, env.current_host):
            _wait_until_master_is_down(env.hosts[0])

    else:
        if env.current_instance_group in env.distribution_instance_groups:
            mpi_enabled = env.additional_framework_parameters.get("sagemaker_mpi_enabled")

            if mpi_enabled:
                runner_type = runner.MPIRunnerType
            elif sagemaker_distributed_dataparallel_enabled:
                runner_type = runner.SMDataParallelRunnerType

        entry_point.run(
            uri=env.module_dir,
            user_entry_point=env.user_entry_point,
            args=cmd_args,
            env_vars=env_vars,
            capture_error=True,
            runner_type=runner_type,
        )


def _log_model_missing_warning(model_dir):
    pb_file_exists = False
    file_exists = False
    for dirpath, dirnames, filenames in os.walk(model_dir):
        if filenames:
            file_exists = True
        for f in filenames:
            if "saved_model.pb" in f or "saved_model.pbtxt" in f:
                pb_file_exists = True
                path, direct_parent_dir = os.path.split(dirpath)
                if not str.isdigit(direct_parent_dir):
                    logger.warn(
                        "Your model will NOT be servable with SageMaker TensorFlow Serving containers. "
                        'The SavedModel bundle is under directory "{}", not a numeric name.'.format(
                            direct_parent_dir
                        )
                    )

    if not file_exists:
        logger.warn(
            "No model artifact is saved under path {}."
            " Your training job will not save any model files to S3.\n"
            "For details of how to construct your training script see:\n"
            "https://sagemaker.readthedocs.io/en/stable/using_tf.html#adapting-your-local-tensorflow-script".format(
                model_dir
            )
        )
    elif not pb_file_exists:
        logger.warn(
            "Your model will NOT be servable with SageMaker TensorFlow Serving container. "
            "The model artifact was not saved in the TensorFlow SavedModel directory structure:\n"
            "https://www.tensorflow.org/guide/saved_model#structure_of_a_savedmodel_directory"
        )


def _model_dir_with_training_job(model_dir, job_name):
    if model_dir and model_dir.startswith("/opt/ml"):
        return model_dir
    else:
        return "{}/{}/model".format(model_dir, job_name)


def main():
    """Training entry point"""
    hyperparameters = environment.read_hyperparameters()
    env = environment.Environment(hyperparameters=hyperparameters)

    user_hyperparameters = env.hyperparameters

    # If the training job is part of the multiple training jobs for tuning, we need to append the training job name to
    # model_dir in case they read from/write to the same object
    if "_tuning_objective_metric" in hyperparameters:
        model_dir = _model_dir_with_training_job(hyperparameters.get("model_dir"), env.job_name)
        logger.info("Appending the training job name to model_dir: {}".format(model_dir))
        user_hyperparameters["model_dir"] = model_dir

    s3_utils.configure(user_hyperparameters.get("model_dir"), os.environ.get("SAGEMAKER_REGION"))
    train(env, mapping.to_cmd_args(user_hyperparameters))
    _log_model_missing_warning(MODEL_DIR)
