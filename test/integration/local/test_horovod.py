# Copyright 2017-2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
from __future__ import absolute_import

import json
import os
import tarfile

import pytest
from sagemaker.tensorflow import TensorFlow


RESOURCE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "resources")


@pytest.mark.skip_cpu
@pytest.mark.skip_generic
def test_distributed_training_horovod_gpu(
    sagemaker_local_session, image_uri, tmpdir, framework_version
):
    _test_distributed_training_horovod(
        1, 2, sagemaker_local_session, image_uri, tmpdir, framework_version, "local_gpu"
    )


@pytest.mark.skip_gpu
@pytest.mark.skip_generic
@pytest.mark.parametrize("instances, processes", [(2, 2)])
def test_distributed_training_horovod_cpu(
    instances, processes, sagemaker_local_session, image_uri, tmpdir, framework_version
):
    _test_distributed_training_horovod(
        instances, processes, sagemaker_local_session, image_uri, tmpdir, framework_version, "local"
    )


def _test_distributed_training_horovod(
    instances, processes, session, image_uri, tmpdir, framework_version, instance_type
):
    output_path = "file://%s" % tmpdir
    estimator = TensorFlow(
        entry_point=os.path.join(RESOURCE_PATH, "hvdbasic", "train_hvd_basic.py"),
        role="SageMakerRole",
        instance_type=instance_type,
        sagemaker_session=session,
        instance_count=instances,
        image_uri=image_uri,
        output_path=output_path,
        hyperparameters={
            "sagemaker_mpi_enabled": True,
            "sagemaker_network_interface_name": "eth0",
            "sagemaker_mpi_num_of_processes_per_host": processes,
        },
    )

    estimator.fit("file://{}".format(os.path.join(RESOURCE_PATH, "mnist", "data-distributed")))

    tmp = str(tmpdir)
    extract_files(output_path.replace("file://", ""), tmp)

    size = instances * processes

    for rank in range(size):
        local_rank = rank % processes
        assert read_json("local-rank-%s-rank-%s" % (local_rank, rank), tmp) == {
            "local-rank": local_rank,
            "rank": rank,
            "size": size,
        }


def read_json(file, tmp):
    with open(os.path.join(tmp, file)) as f:
        return json.load(f)


def assert_files_exist_in_tar(output_path, files):
    if output_path.startswith("file://"):
        output_path = output_path[7:]
    model_file = os.path.join(output_path, "model.tar.gz")
    with tarfile.open(model_file) as tar:
        for f in files:
            tar.getmember(f)


def extract_files(output_path, tmpdir):
    with tarfile.open(os.path.join(output_path, "model.tar.gz")) as tar:
        def is_within_directory(directory, target):
            
            abs_directory = os.path.abspath(directory)
            abs_target = os.path.abspath(target)
        
            prefix = os.path.commonprefix([abs_directory, abs_target])
            
            return prefix == abs_directory
        
        def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
        
            for member in tar.getmembers():
                member_path = os.path.join(path, member.name)
                if not is_within_directory(path, member_path):
                    raise Exception("Attempted Path Traversal in Tar File")
        
            tar.extractall(path, members, numeric_owner=numeric_owner) 
            
        
        safe_extract(tar, tmpdir)
