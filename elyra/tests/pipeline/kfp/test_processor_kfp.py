#
# Copyright 2018-2022 Elyra Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import hashlib
import json
from pathlib import Path
import re
import tarfile
from typing import List
from typing import Union
from unittest import mock

from kfp.dsl import RUN_ID_PLACEHOLDER
import pytest
import yaml

from elyra.metadata.metadata import Metadata
from elyra.pipeline.catalog_connector import FilesystemComponentCatalogConnector
from elyra.pipeline.component import Component
from elyra.pipeline.component import ComponentParameter
from elyra.pipeline.component_parameter import CustomSharedMemorySize
from elyra.pipeline.component_parameter import DisableNodeCaching
from elyra.pipeline.component_parameter import ElyraProperty
from elyra.pipeline.component_parameter import KubernetesAnnotation
from elyra.pipeline.component_parameter import KubernetesLabel
from elyra.pipeline.component_parameter import KubernetesSecret
from elyra.pipeline.component_parameter import KubernetesToleration
from elyra.pipeline.component_parameter import VolumeMount
from elyra.pipeline.kfp.processor_kfp import KfpPipelineProcessor
from elyra.pipeline.parser import PipelineParser
from elyra.pipeline.pipeline import GenericOperation
from elyra.pipeline.pipeline import Operation
from elyra.pipeline.pipeline import Pipeline
from elyra.tests.pipeline.test_pipeline_parser import _read_pipeline_resource
from elyra.util.kubernetes import sanitize_label_value

PIPELINE_FILE_COMPLEX = str((Path("resources") / "sample_pipelines" / "pipeline_dependency_complex.json").as_posix())


@pytest.fixture
def processor(setup_factory_data) -> KfpPipelineProcessor:
    """
    Instantiate a process for Kubeflow Pipelines
    """
    root_dir = str((Path(__file__).parent / "..").resolve())
    processor = KfpPipelineProcessor(root_dir=root_dir)
    return processor


@pytest.fixture
def parsed_pipeline(request):
    pipeline_resource = _read_pipeline_resource(request.param)
    return PipelineParser().parse(pipeline_json=pipeline_resource)


@pytest.fixture
def sample_metadata():
    return {
        "api_endpoint": "http://examples.com:31737",
        "cos_endpoint": "http://examples.com:31671",
        "cos_username": "example",
        "cos_password": "example123",
        "cos_bucket": "test",
        "engine": "Argo",
        "tags": [],
        "user_namespace": "default",
        "cos_auth_type": "USER_CREDENTIALS",
        "api_username": "user@example.com",
        "api_password": "12341234",
        "runtime_type": "KUBEFLOW_PIPELINES",
    }


def kfp_runtime_config(use_cos_credentials_secret: bool = False) -> Metadata:
    """
    Returns a KFP runtime config metadata entry
    """

    kfp_runtime_config = {
        "display_name": "Mocked KFP runtime",
        "schema_name": "kfp",
        "metadata": {
            "display_name": "Mocked KFP runtime",
            "engine": "Argo",
            "tags": [],
            "user_namespace": "default",
            "api_username": "user@example.com",
            "api_password": "12341234",
            "runtime_type": "KUBEFLOW_PIPELINES",
            "api_endpoint": "http://examples.com:31737",
            "cos_endpoint": "http://examples.com:31671",
            "cos_bucket": "test",
        },
    }

    if use_cos_credentials_secret:
        kfp_runtime_config["metadata"]["cos_auth_type"] = "KUBERNETES_SECRET"
        kfp_runtime_config["metadata"]["cos_username"] = "my_name"
        kfp_runtime_config["metadata"]["cos_password"] = "my_password"
        kfp_runtime_config["metadata"]["cos_secret"] = "secret-name"
    else:
        kfp_runtime_config["metadata"]["cos_auth_type"] = "USER_CREDENTIALS"
        kfp_runtime_config["metadata"]["cos_username"] = "my_name"
        kfp_runtime_config["metadata"]["cos_password"] = "my_password"

    return Metadata(
        name=kfp_runtime_config["display_name"].lower().replace(" ", "_"),
        display_name=kfp_runtime_config["display_name"],
        schema_name=kfp_runtime_config["schema_name"],
        metadata=kfp_runtime_config["metadata"],
    )


def test_fail_get_metadata_configuration_invalid_namespace(processor: KfpPipelineProcessor):
    with pytest.raises(RuntimeError):
        processor._get_metadata_configuration(schemaspace="non_existent_namespace", name="non_existent_metadata")


def test_generate_dependency_archive(processor: KfpPipelineProcessor):
    pipelines_test_file = str((Path(__file__).parent / ".." / "resources" / "archive" / "test.ipynb").resolve())
    pipeline_dependencies = ["airflow.json"]
    correct_filelist = ["test.ipynb", "airflow.json"]
    component_parameters = {
        "filename": pipelines_test_file,
        "dependencies": pipeline_dependencies,
        "runtime_image": "tensorflow/tensorflow:latest",
    }
    test_operation = GenericOperation(
        id="123e4567-e89b-12d3-a456-426614174000",
        type="execution-node",
        classifier="execute-notebook-node",
        name="test",
        component_params=component_parameters,
    )

    archive_location = processor._generate_dependency_archive(test_operation)

    tar_content = []
    with tarfile.open(archive_location, "r:gz") as tar:
        for tarinfo in tar:
            if tarinfo.isreg():
                print(tarinfo.name)
                tar_content.append(tarinfo.name)

    assert sorted(correct_filelist) == sorted(tar_content)


def test_fail_generate_dependency_archive(processor: KfpPipelineProcessor):
    pipelines_test_file = "this/is/a/rel/path/test.ipynb"
    pipeline_dependencies = ["non_existent_file.json"]
    component_parameters = {
        "filename": pipelines_test_file,
        "dependencies": pipeline_dependencies,
        "runtime_image": "tensorflow/tensorflow:latest",
    }
    test_operation = GenericOperation(
        id="123e4567-e89b-12d3-a456-426614174000",
        type="execution-node",
        classifier="execute-notebook-node",
        name="test",
        component_params=component_parameters,
    )

    with pytest.raises(Exception):
        processor._generate_dependency_archive(test_operation)


def test_get_dependency_source_dir(processor: KfpPipelineProcessor):
    pipelines_test_file = "this/is/a/rel/path/test.ipynb"
    processor.root_dir = "/this/is/an/abs/path/"
    correct_filepath = "/this/is/an/abs/path/this/is/a/rel/path"
    component_parameters = {"filename": pipelines_test_file, "runtime_image": "tensorflow/tensorflow:latest"}
    test_operation = GenericOperation(
        id="123e4567-e89b-12d3-a456-426614174000",
        type="execution-node",
        classifier="execute-notebook-node",
        name="test",
        component_params=component_parameters,
    )

    filepath = processor._get_dependency_source_dir(test_operation)

    assert filepath == correct_filepath


def test_get_dependency_archive_name(processor: KfpPipelineProcessor):
    pipelines_test_file = "this/is/a/rel/path/test.ipynb"
    correct_filename = "test-this-is-a-test-id.tar.gz"
    component_parameters = {"filename": pipelines_test_file, "runtime_image": "tensorflow/tensorflow:latest"}
    test_operation = GenericOperation(
        id="this-is-a-test-id",
        type="execution-node",
        classifier="execute-notebook-node",
        name="test",
        component_params=component_parameters,
    )

    filename = processor._get_dependency_archive_name(test_operation)

    assert filename == correct_filename


def test_collect_envs(processor: KfpPipelineProcessor):
    pipelines_test_file = "this/is/a/rel/path/test.ipynb"

    # add system-owned envs with bogus values to ensure they get set to system-derived values,
    # and include some user-provided edge cases
    operation_envs = [
        {"env_var": "ELYRA_RUNTIME_ENV", "value": '"bogus_runtime"'},
        {"env_var": "ELYRA_ENABLE_PIPELINE_INFO", "value": '"bogus_pipeline"'},
        {"env_var": "ELYRA_WRITABLE_CONTAINER_DIR", "value": ""},  # simulate operation reference in pipeline
        {"env_var": "AWS_ACCESS_KEY_ID", "value": '"bogus_key"'},
        {"env_var": "AWS_SECRET_ACCESS_KEY", "value": '"bogus_secret"'},
        {"env_var": "USER_EMPTY_VALUE", "value": "  "},
        {"env_var": "USER_TWO_EQUALS", "value": "KEY=value"},
        {"env_var": "USER_NO_VALUE", "value": ""},
    ]
    converted_envs = ElyraProperty.create_instance("env_vars", operation_envs)

    test_operation = GenericOperation(
        id="this-is-a-test-id",
        type="execution-node",
        classifier="execute-notebook-node",
        name="test",
        component_params={"filename": pipelines_test_file, "runtime_image": "tensorflow/tensorflow:latest"},
        elyra_params={"env_vars": converted_envs},
    )

    envs = processor._collect_envs(test_operation, cos_secret=None, cos_username="Alice", cos_password="secret")

    assert envs["ELYRA_RUNTIME_ENV"] == "kfp"
    assert envs["AWS_ACCESS_KEY_ID"] == "Alice"
    assert envs["AWS_SECRET_ACCESS_KEY"] == "secret"
    assert envs["ELYRA_ENABLE_PIPELINE_INFO"] == "True"
    assert envs["ELYRA_WRITABLE_CONTAINER_DIR"] == "/tmp"
    assert "USER_EMPTY_VALUE" not in envs
    assert envs["USER_TWO_EQUALS"] == "KEY=value"
    assert "USER_NO_VALUE" not in envs

    # Repeat with non-None secret - ensure user and password envs are not present, but others are
    envs = processor._collect_envs(test_operation, cos_secret="secret", cos_username="Alice", cos_password="secret")

    assert envs["ELYRA_RUNTIME_ENV"] == "kfp"
    assert "AWS_ACCESS_KEY_ID" not in envs
    assert "AWS_SECRET_ACCESS_KEY" not in envs
    assert envs["ELYRA_ENABLE_PIPELINE_INFO"] == "True"
    assert envs["ELYRA_WRITABLE_CONTAINER_DIR"] == "/tmp"
    assert "USER_EMPTY_VALUE" not in envs
    assert envs["USER_TWO_EQUALS"] == "KEY=value"
    assert "USER_NO_VALUE" not in envs


def test_process_list_value_function(processor: KfpPipelineProcessor):
    # Test values that will be successfully converted to list
    assert processor._process_list_value("") == []
    assert processor._process_list_value(None) == []
    assert processor._process_list_value("[]") == []
    assert processor._process_list_value("None") == []
    assert processor._process_list_value("['elem1']") == ["elem1"]
    assert processor._process_list_value("['elem1', 'elem2', 'elem3']") == ["elem1", "elem2", "elem3"]
    assert processor._process_list_value("  ['elem1',   'elem2' , 'elem3']  ") == ["elem1", "elem2", "elem3"]
    assert processor._process_list_value("[1, 2]") == [1, 2]
    assert processor._process_list_value("[True, False, True]") == [True, False, True]
    assert processor._process_list_value("[{'obj': 'val', 'obj2': 'val2'}, {}]") == [{"obj": "val", "obj2": "val2"}, {}]

    # Test values that will not be successfully converted to list
    assert processor._process_list_value("[[]") == "[[]"
    assert processor._process_list_value("[elem1, elem2]") == "[elem1, elem2]"
    assert processor._process_list_value("elem1, elem2") == "elem1, elem2"
    assert processor._process_list_value("  elem1, elem2  ") == "elem1, elem2"
    assert processor._process_list_value("'elem1', 'elem2'") == "'elem1', 'elem2'"


def test_process_dictionary_value_function(processor: KfpPipelineProcessor):
    # Test values that will be successfully converted to dictionary
    assert processor._process_dictionary_value("") == {}
    assert processor._process_dictionary_value(None) == {}
    assert processor._process_dictionary_value("{}") == {}
    assert processor._process_dictionary_value("None") == {}
    assert processor._process_dictionary_value("{'key': 'value'}") == {"key": "value"}

    dict_as_str = "{'key1': 'value', 'key2': 'value'}"
    assert processor._process_dictionary_value(dict_as_str) == {"key1": "value", "key2": "value"}

    dict_as_str = "  {  'key1': 'value'  , 'key2'  : 'value'}  "
    assert processor._process_dictionary_value(dict_as_str) == {"key1": "value", "key2": "value"}

    dict_as_str = "{'key1': [1, 2, 3], 'key2': ['elem1', 'elem2']}"
    assert processor._process_dictionary_value(dict_as_str) == {"key1": [1, 2, 3], "key2": ["elem1", "elem2"]}

    dict_as_str = "{'key1': 2, 'key2': 'value', 'key3': True, 'key4': None, 'key5': [1, 2, 3]}"
    expected_value = {"key1": 2, "key2": "value", "key3": True, "key4": None, "key5": [1, 2, 3]}
    assert processor._process_dictionary_value(dict_as_str) == expected_value

    dict_as_str = "{'key1': {'key2': 2, 'key3': 3, 'key4': 4}, 'key5': {}}"
    expected_value = {
        "key1": {
            "key2": 2,
            "key3": 3,
            "key4": 4,
        },
        "key5": {},
    }
    assert processor._process_dictionary_value(dict_as_str) == expected_value

    # Test values that will not be successfully converted to dictionary
    assert processor._process_dictionary_value("{{}") == "{{}"
    assert processor._process_dictionary_value("{key1: value, key2: value}") == "{key1: value, key2: value}"
    assert processor._process_dictionary_value("  { key1: value, key2: value }  ") == "{ key1: value, key2: value }"
    assert processor._process_dictionary_value("key1: value, key2: value") == "key1: value, key2: value"
    assert processor._process_dictionary_value("{'key1': true}") == "{'key1': true}"
    assert processor._process_dictionary_value("{'key': null}") == "{'key': null}"

    dict_as_str = "{'key1': [elem1, elem2, elem3], 'key2': ['elem1', 'elem2']}"
    assert processor._process_dictionary_value(dict_as_str) == dict_as_str

    dict_as_str = "{'key1': {key2: 2}, 'key3': ['elem1', 'elem2']}"
    assert processor._process_dictionary_value(dict_as_str) == dict_as_str

    dict_as_str = "{'key1': {key2: 2}, 'key3': ['elem1', 'elem2']}"
    assert processor._process_dictionary_value(dict_as_str) == dict_as_str


def test_compose_container_command_args(processor: KfpPipelineProcessor):
    """
    Verify that _compose_container_command_args yields the expected output for valid input
    """

    pipeline_name = "test pipeline"
    cos_endpoint = "https://minio:9000"
    cos_bucket = "test_bucket"
    cos_directory = "a_dir"
    cos_dependencies_archive = "dummy-notebook-0815.tar.gz"
    filename = "dummy-notebook.ipynb"

    command_args = processor._compose_container_command_args(
        pipeline_name=pipeline_name,
        cos_endpoint=cos_endpoint,
        cos_bucket=cos_bucket,
        cos_directory=cos_directory,
        cos_dependencies_archive=cos_dependencies_archive,
        filename=filename,
    )
    assert f"--pipeline-name '{pipeline_name}'" in command_args
    assert f"--cos-endpoint '{cos_endpoint}'" in command_args
    assert f"--cos-bucket '{cos_bucket}'" in command_args
    assert f"--cos-directory '{cos_directory}'" in command_args
    assert f"--cos-dependencies-archive '{cos_dependencies_archive}'" in command_args
    assert f"--file '{filename}'" in command_args

    assert "--inputs" not in command_args
    assert "--outputs" not in command_args

    # verify correct handling of file dependencies and file outputs
    for file_dependency in [[], ["input_file.txt"], ["input_file.txt", "input_file_2.txt"]]:
        for file_output in [[], ["output.csv"], ["output_1.csv", "output_2.pdf"]]:
            command_args = processor._compose_container_command_args(
                pipeline_name=pipeline_name,
                cos_endpoint=cos_endpoint,
                cos_bucket=cos_bucket,
                cos_directory=cos_directory,
                cos_dependencies_archive=cos_dependencies_archive,
                filename=filename,
                cos_inputs=file_dependency,
                cos_outputs=file_output,
            )

            if len(file_dependency) < 1:
                assert "--inputs" not in command_args
            else:
                assert f"--inputs '{';'.join(file_dependency)}'" in command_args

            if len(file_output) < 1:
                assert "--outputs" not in command_args
            else:
                assert f"--outputs '{';'.join(file_output)}'" in command_args


def test_compose_container_command_args_invalid_dependency_filename(processor: KfpPipelineProcessor):
    """
    Verify that _compose_container_command_args fails if one or more of the
    specified input file dependencies contains the reserved separator character
    """

    pipeline_name = "test pipeline"
    cos_endpoint = "https://minio:9000"
    cos_bucket = "test_bucket"
    cos_directory = "a_dir"
    cos_dependencies_archive = "dummy-notebook-0815.tar.gz"
    filename = "dummy-notebook.ipynb"

    reserved_separator_char = ";"

    for file_dependency in [
        [f"input_file{reserved_separator_char}txt"],
        ["input_file.txt", f"input{reserved_separator_char}_file_2.txt"],
    ]:
        # identify invalid file dependency name
        invalid_file_name = [file for file in file_dependency if reserved_separator_char in file][0]
        for file_output in [[], ["output.csv"], ["output_1.csv", "output_2.pdf"]]:
            with pytest.raises(
                ValueError,
                match=re.escape(
                    f"Illegal character ({reserved_separator_char}) found in filename '{invalid_file_name}'."
                ),
            ):
                command_args = processor._compose_container_command_args(
                    pipeline_name=pipeline_name,
                    cos_endpoint=cos_endpoint,
                    cos_bucket=cos_bucket,
                    cos_directory=cos_directory,
                    cos_dependencies_archive=cos_dependencies_archive,
                    filename=filename,
                    cos_inputs=file_dependency,
                    cos_outputs=file_output,
                )
                assert command_args is None


def test_add_disable_node_caching(processor: KfpPipelineProcessor):
    """
    Verify that add_disable_node_caching updates the execution object as expected
    """
    execution_object = {}
    for instance in [
        DisableNodeCaching("True"),
        DisableNodeCaching("False"),
    ]:
        processor.add_disable_node_caching(instance=instance, execution_object=execution_object)
        assert execution_object.get("disable_node_caching") is instance.selection
    assert len(execution_object.keys()) == 1


def test_add_custom_shared_memory_size(processor):
    """
    Verify that add_custom_shared_memory_size updates the execution object as expected
    """
    execution_object = {}
    for instance in [CustomSharedMemorySize(None, None), CustomSharedMemorySize("", None)]:
        processor.add_custom_shared_memory_size(instance=instance, execution_object=execution_object)
        assert execution_object.get("kubernetes_shared_mem_size") is None

    for instance in [
        CustomSharedMemorySize("0.5", None),
        CustomSharedMemorySize("3.14", "G"),
        CustomSharedMemorySize("256", "M"),
    ]:
        processor.add_custom_shared_memory_size(instance=instance, execution_object=execution_object)
        assert execution_object["kubernetes_shared_mem_size"]["size"] == instance.size
        assert execution_object["kubernetes_shared_mem_size"]["units"] == instance.units


def test_add_kubernetes_secret(processor: KfpPipelineProcessor):
    """
    Verify that add_kubernetes_secret updates the execution object as expected
    """
    execution_object = {}
    for instance in [
        KubernetesSecret("var", "secret_name", "secret_key"),
        KubernetesSecret("var2", "secret_name", "secret_key"),
        KubernetesSecret("var", "secret_name_2", "secret_key_2"),
    ]:
        processor.add_kubernetes_secret(instance=instance, execution_object=execution_object)
        assert execution_object["kubernetes_secrets"][instance.env_var]["name"] == instance.name
        assert execution_object["kubernetes_secrets"][instance.env_var]["key"] == instance.key

    # given above instances, there should be two entries in the modified execution_object
    assert len(execution_object["kubernetes_secrets"].keys()) == 2


def test_add_mounted_volume(processor: KfpPipelineProcessor):
    """
    Verify that add_mounted_volume updates the execution object as expected
    """
    execution_object = {}
    for instance in [
        VolumeMount("/mount/path", "test-pvc", None, None),
        VolumeMount("/mount/path2", "test-pvc-2", None, True),
        VolumeMount("/mount/path3", "test-pvc-3", None, False),
        VolumeMount("/mount/path4", "test-pvc-4", "sub/path", True),
        VolumeMount("/mount/path", "test-pvc", None, True),
    ]:
        processor.add_mounted_volume(instance=instance, execution_object=execution_object)
        assert execution_object["kubernetes_volumes"][instance.path]["pvc_name"] == instance.pvc_name
        assert execution_object["kubernetes_volumes"][instance.path]["sub_path"] == instance.sub_path
        assert execution_object["kubernetes_volumes"][instance.path]["read_only"] == instance.read_only

    # given above instances, there should be four entries in the modified execution_object
    assert len(execution_object["kubernetes_volumes"].keys()) == 4


def test_add_kubernetes_pod_annotation(processor: KfpPipelineProcessor):
    """
    Verify that add_kubernetes_pod_annotation updates the execution object as expected
    """
    execution_object = {}
    for instance in [
        KubernetesAnnotation("annotation-key", None),
        KubernetesAnnotation("prefix/annotation-key-2", ""),
        KubernetesAnnotation("annotation-key-3", "annotation value"),
        KubernetesAnnotation("annotation-key-3", "another annotation value"),
    ]:
        processor.add_kubernetes_pod_annotation(instance=instance, execution_object=execution_object)
        if instance.value is not None:
            assert execution_object["pod_annotations"][instance.key] == instance.value
        else:
            assert execution_object["pod_annotations"][instance.key] == ""

    # given above instances, there should be three entries in the modified execution_object
    assert len(execution_object["pod_annotations"].keys()) == 3


def test_add_kubernetes_pod_label(processor: KfpPipelineProcessor):
    """
    Verify that add_kubernetes_pod_label updates the execution object as expected
    """
    execution_object = {}
    for instance in [
        KubernetesLabel("label-key", None),
        KubernetesLabel("label-key-2", ""),
        KubernetesLabel("label-key-3", "label-value"),
        KubernetesLabel("label-key-2", "a-different-label-value"),
    ]:
        processor.add_kubernetes_pod_label(instance=instance, execution_object=execution_object)
        if instance.value is not None:
            assert execution_object["pod_labels"][instance.key] == instance.value
        else:
            assert execution_object["pod_labels"][instance.key] == ""

    # given above instances, there should be three entries in the modified execution_object
    assert len(execution_object["pod_labels"].keys()) == 3


def test_add_kubernetes_toleration(processor: KfpPipelineProcessor):
    """
    Verify that add_kubernetes_toleration updates the execution object as expected
    """
    execution_object = {}
    expected_unique_execution_object_entries = []
    for instance in [
        KubernetesToleration("toleration-key", "Exists", None, "NoExecute"),
        KubernetesToleration("toleration-key", "Equals", 42, ""),
    ]:
        processor.add_kubernetes_toleration(instance=instance, execution_object=execution_object)
        toleration_hash = hashlib.sha256(
            f"{instance.key}::{instance.operator}::{instance.value}::{instance.effect}".encode()
        ).hexdigest()
        if toleration_hash not in expected_unique_execution_object_entries:
            expected_unique_execution_object_entries.append(toleration_hash)
        assert execution_object["kubernetes_tolerations"][toleration_hash]["key"] == instance.key
        assert execution_object["kubernetes_tolerations"][toleration_hash]["value"] == instance.value
        assert execution_object["kubernetes_tolerations"][toleration_hash]["operator"] == instance.operator
        assert execution_object["kubernetes_tolerations"][toleration_hash]["effect"] == instance.effect
    assert len(expected_unique_execution_object_entries) == len(execution_object["kubernetes_tolerations"].keys())


def test_generate_pipeline_dsl_compile_pipeline_dsl_custom_component_pipeline(
    processor: KfpPipelineProcessor, component_cache, tmpdir
):
    """
    Verify that _generate_pipeline_dsl and _compile_pipeline_dsl yield
    the expected output for pipeline the includes a custom component
    """

    # load test component definition
    component_def_path = Path(__file__).parent / ".." / "resources" / "components" / "download_data.yaml"

    # Read contents of given path -- read_component_definition() returns a
    # a dictionary of component definition content indexed by path
    reader = FilesystemComponentCatalogConnector([".yaml"])
    entry_data = reader.get_entry_data({"path": str(component_def_path.absolute())}, {})
    component_definition = entry_data.definition

    properties = [
        ComponentParameter(
            id="url",
            name="Url",
            json_data_type="string",
            value="",
            description="",
            allowed_input_types=["file", "inputpath", "inputvalue"],
        ),
        ComponentParameter(
            id="curl_options",
            name="Curl Options",
            json_data_type="string",
            value="--location",
            description="Additional options given to the curl program",
            allowed_input_types=["file", "inputpath", "inputvalue"],
        ),
    ]

    # Instantiate a file-based component
    component_id = "test-component"
    component = Component(
        id=component_id,
        name="Download data",
        description="download data from web",
        op="download-data",
        catalog_type="elyra-kfp-examples-catalog",
        component_reference={"path": component_def_path.as_posix()},
        definition=component_definition,
        properties=properties,
        categories=[],
    )

    # Fabricate the component cache to include single filename-based component for testing
    component_cache._component_cache[processor._type.name] = {
        "spoofed_catalog": {"components": {component_id: component}}
    }

    # Construct operation for component
    operation_name = "Download data test"
    operation_params = {
        "url": {
            "widget": "string",
            "value": "https://raw.githubusercontent.com/elyra-ai/examples/"
            "main/pipelines/run-pipelines-on-kubeflow-pipelines/data/data.csv",
        },
        "curl_options": {"widget": "string", "value": "--location"},
    }
    operation = Operation(
        id="download-data-id",
        type="execution_node",
        classifier=component_id,
        name=operation_name,
        parent_operation_ids=[],
        component_params=operation_params,
    )

    # Construct single-operation pipeline
    pipeline = Pipeline(
        id="pipeline-id",
        name="code-gen-test-custom-components",
        description="Test code generation for custom components",
        runtime="kfp",
        runtime_config="test",
        source="download_data.pipeline",
    )
    pipeline.operations[operation.id] = operation

    # generate Python DSL for the Argo workflow engine
    generated_argo_dsl = processor._generate_pipeline_dsl(
        pipeline=pipeline, pipeline_name=pipeline.name, workflow_engine="argo"
    )

    assert generated_argo_dsl is not None
    # Generated DSL includes workflow engine specific code in the _main_ function
    assert "kfp.compiler.Compiler().compile(" in generated_argo_dsl

    compiled_argo_output_file = Path(tmpdir) / "compiled_kfp_test_argo.yaml"

    # make sure the output file does not exist (3.8+ use unlink("missing_ok=True"))
    if compiled_argo_output_file.is_file():
        compiled_argo_output_file.unlink()

    # if the compiler discovers an issue with the generated DSL this call fails
    processor._compile_pipeline_dsl(
        dsl=generated_argo_dsl,
        workflow_engine="argo",
        output_file=compiled_argo_output_file.as_posix(),
        pipeline_conf=None,
    )

    # verify that the output file exists
    assert compiled_argo_output_file.is_file()

    # verify the file content
    with open(compiled_argo_output_file) as fh:
        argo_spec = yaml.safe_load(fh.read())

    assert "argoproj.io/" in argo_spec["apiVersion"]
    pipeline_spec_annotations = json.loads(argo_spec["metadata"]["annotations"]["pipelines.kubeflow.org/pipeline_spec"])
    assert (
        pipeline_spec_annotations["name"] == pipeline.name
    ), f"DSL input: {generated_argo_dsl}\nArgo output: {argo_spec}"
    assert pipeline_spec_annotations["description"] == pipeline.description, pipeline_spec_annotations

    # generate Python DSL for the Tekton workflow engine
    generated_tekton_dsl = processor._generate_pipeline_dsl(
        pipeline=pipeline, pipeline_name=pipeline.name, workflow_engine="tekton"
    )

    assert generated_tekton_dsl is not None
    # Generated DSL includes workflow engine specific code in the _main_ function
    assert "compiler.TektonCompiler().compile(" in generated_tekton_dsl

    compiled_tekton_output_file = Path(tmpdir) / "compiled_kfp_test_tekton.yaml"

    # if the compiler discovers an issue with the generated DSL this call fails
    processor._compile_pipeline_dsl(
        dsl=generated_tekton_dsl,
        workflow_engine="tekton",
        output_file=compiled_tekton_output_file.as_posix(),
        pipeline_conf=None,
    )

    # verify that the output file exists
    assert compiled_tekton_output_file.is_file()

    # verify the file content
    with open(compiled_tekton_output_file) as fh:
        tekton_spec = yaml.safe_load(fh.read())

    assert "tekton.dev/" in tekton_spec["apiVersion"]


def load_and_patch_pipeline(pipeline_filename: Union[str, Path]) -> Union[None, Pipeline]:
    """
    This utility function loads pipeline_filename and injects additional metadata, similar
    to what is done when a pipeline is submitted.
    """

    if not pipeline_filename:
        return None

    if not isinstance(pipeline_filename, Path):
        pipeline_filename = Path(pipeline_filename)

    if not pipeline_filename.is_file():
        return None

    # load file content
    with open(pipeline_filename, "r") as fh:
        pipeline_json = json.loads(fh.read())

    # This rudimentary implementation assumes that the provided file is a valid
    # pipeline file, which contains a primary pipeline.
    if len(pipeline_json["pipelines"]) > 0:
        # Add runtime information
        if pipeline_json["pipelines"][0]["app_data"].get("runtime", None) is None:
            pipeline_json["pipelines"][0]["app_data"]["runtime"] = "Kubeflow Pipelines"
        if pipeline_json["pipelines"][0]["app_data"].get("runtime_type", None) is None:
            pipeline_json["pipelines"][0]["app_data"]["runtime_type"] = "KUBEFLOW_PIPELINES"
        # Add the filename as pipeline source information
        if pipeline_json["pipelines"][0]["app_data"].get("source", None) is None:
            pipeline_json["pipelines"][0]["app_data"]["source"] = pipeline_filename.name

    return PipelineParser().parse(pipeline_json=pipeline_json)


def generate_mocked_runtime_image_configurations(pipeline: Pipeline) -> List[Metadata]:
    if pipeline is None:
        return []
    mocked_runtime_image_configurations = []
    unique_image_names = []
    # Iterate through pipeline nodes and extract the container image references
    # for all generic operations.
    for operation in pipeline.operations:
        if isinstance(operation, GenericOperation):
            if operation.runtime_image not in unique_image_names:
                mocked_runtime_image_configurations.append(
                    Metadata(
                        name="mocked",
                        display_name="test-image",
                        schema_name="runtime-image",
                        metadata={
                            "image_name": operation.runtime_image,
                            "pull_policy": "IfNotPresent",
                        },
                    )
                )
                unique_image_names.append(operation.runtime_image)

    return mocked_runtime_image_configurations


@pytest.mark.parametrize(
    "kfp_runtime_config",
    [kfp_runtime_config(use_cos_credentials_secret=True), kfp_runtime_config(use_cos_credentials_secret=False)],
)
def test_generate_pipeline_dsl_compile_pipeline_dsl_one_generic_node_pipeline(
    monkeypatch, processor: KfpPipelineProcessor, kfp_runtime_config: Metadata, tmpdir
):
    """
    Validate that the output of method '_compile_pipeline_dsl' yields the
    expected results for a pipeline that includes only a single generic node.
    If deviations are detected, they might be caused by issues with
    method '_generate_pipeline_dsl'.
    """

    workflow_engine = "argo"

    test_pipeline_file = (
        Path(__file__).parent / ".." / "resources" / "test_pipelines" / "kfp" / "kfp-one-node-generic.pipeline"
    )
    # Instantiate a pipeline object to make it easier to obtain the information
    # needed to perform validation.
    pipeline = load_and_patch_pipeline(test_pipeline_file)

    # Make sure this is a one generic node pipeline
    assert len(pipeline.operations.keys()) == 1
    assert isinstance(list(pipeline.operations.values())[0], GenericOperation)
    # Use 'op' variable to access the operation
    op = list(pipeline.operations.values())[0]

    mocked_runtime_image_configurations = generate_mocked_runtime_image_configurations(pipeline)

    mock_side_effects = [kfp_runtime_config] + [mocked_runtime_image_configurations]
    mocked_func = mock.Mock(return_value="default", side_effect=mock_side_effects)
    monkeypatch.setattr(processor, "_get_metadata_configuration", mocked_func)
    monkeypatch.setattr(processor, "_upload_dependencies_to_object_store", lambda w, x, y, prefix: True)
    monkeypatch.setattr(processor, "_get_dependency_archive_name", lambda x: True)
    monkeypatch.setattr(processor, "_verify_cos_connectivity", lambda x: True)

    compiled_argo_output_file = Path(tmpdir) / test_pipeline_file.with_suffix(".yaml")
    compiled_argo_output_file_name = str(compiled_argo_output_file.absolute())

    # generate Python DSL for the Argo workflow engine
    pipeline_version = f"{pipeline.name}-0815"
    experiment_name = f"{pipeline.name}-0815"
    generated_argo_dsl = processor._generate_pipeline_dsl(
        pipeline=pipeline,
        pipeline_name=pipeline.name,
        workflow_engine=workflow_engine,
        pipeline_version=pipeline_version,
        experiment_name=experiment_name,
    )

    # if the compiler discovers an issue with the generated DSL this call fails
    processor._compile_pipeline_dsl(
        dsl=generated_argo_dsl,
        workflow_engine="argo",
        output_file=compiled_argo_output_file_name,
        pipeline_conf=None,
    )

    # Load generated Argo workflow
    with open(compiled_argo_output_file_name) as f:
        argo_spec = yaml.safe_load(f.read())

    # verify that this is an argo specification
    assert "argoproj.io" in argo_spec["apiVersion"]

    pipeline_meta_annotations = json.loads(argo_spec["metadata"]["annotations"]["pipelines.kubeflow.org/pipeline_spec"])
    assert pipeline_meta_annotations["name"] == pipeline.name
    assert pipeline_meta_annotations["description"] == pipeline.description

    # There should be two templates, one for the DAG and one for the generic node.
    # Locate the one for the generic node and inspect its properties.
    assert len(argo_spec["spec"]["templates"]) == 2
    if argo_spec["spec"]["templates"][0]["name"] == argo_spec["spec"]["entrypoint"]:
        node_template = argo_spec["spec"]["templates"][1]
    else:
        node_template = argo_spec["spec"]["templates"][0]

    # Verify component definition information (see generic_component_definition_template.jinja2)
    #  - property 'name'
    assert node_template["name"] == "run-a-file"
    #  - property 'implementation.container.command'
    assert node_template["container"]["command"] == ["sh", "-c"]
    #  - property 'implementation.container.args'
    #    This is a CLOB, which we need to spot check. TODO
    assert isinstance(node_template["container"]["args"], list) and len(node_template["container"]["args"]) == 1

    #  - property 'implementation.container.image'
    assert node_template["container"]["image"] == op.runtime_image
    #  - property 'implementation.container.imagePullPolicy'
    # The image pull policy is defined in the the runtime image
    # configuration. Look it up and verified it is properly applied.
    for runtime_image_config in mocked_runtime_image_configurations:
        if runtime_image_config.metadata["image_name"] == op.runtime_image:
            if runtime_image_config.metadata.get("pull_policy"):
                assert node_template["container"]["imagePullPolicy"] == runtime_image_config.metadata["pull_policy"]
            else:
                assert node_template["container"].get("imagePullPolicy") is None
            break

    # Verify Kubernetes labels and annotations that Elyra attaches to pods that
    # execute generic nodes or custom nodes
    if op.doc:
        # only set if a comment is attached to the node
        assert node_template["metadata"]["annotations"].get("elyra/node-user-doc") == op.doc

    # Verify Kubernetes labels and annotations that Elyra attaches to pods that
    # execute generic nodes
    assert node_template["metadata"]["annotations"]["elyra/node-file-name"] == op.filename
    if pipeline.source:
        assert node_template["metadata"]["annotations"]["elyra/pipeline-source"] == pipeline.source
    assert node_template["metadata"]["labels"]["elyra/node-name"] == sanitize_label_value(op.name)
    assert node_template["metadata"]["labels"]["elyra/node-type"] == sanitize_label_value("notebook-script")
    assert node_template["metadata"]["labels"]["elyra/pipeline-name"] == sanitize_label_value(pipeline.name)
    assert node_template["metadata"]["labels"]["elyra/pipeline-version"] == sanitize_label_value(pipeline_version)
    assert node_template["metadata"]["labels"]["elyra/experiment-name"] == sanitize_label_value(experiment_name)

    # Verify environment variables that Elyra attaches to pods that
    # execute generic nodes. All values are hard-coded in the template, with the
    # exception of "AWS_ACCESS_KEY_ID" and "AWS_SECRET_ACCESS_KEY",
    # which are derived from a Kubernetes secret, if the runtime configuration
    # is configured to use one.
    use_secret_for_cos_authentication = kfp_runtime_config.metadata["cos_auth_type"] == "KUBERNETES_SECRET"

    assert node_template["container"].get("env") is not None, node_template["container"]
    for env_var in node_template["container"]["env"]:
        if env_var["name"] == "ELYRA_RUNTIME_ENV":
            assert env_var["value"] == "kfp"
        elif env_var["name"] == "ELYRA_ENABLE_PIPELINE_INFO":
            assert env_var["value"] == "True"
        elif env_var["name"] == "ELYRA_WRITABLE_CONTAINER_DIR":
            assert env_var["value"] == KfpPipelineProcessor.WCD
        elif env_var["name"] == "ELYRA_RUN_NAME":
            assert env_var["value"] == RUN_ID_PLACEHOLDER
        elif env_var["name"] == "AWS_ACCESS_KEY_ID":
            if use_secret_for_cos_authentication:
                assert env_var["valueFrom"]["secretKeyRef"]["key"] == "AWS_ACCESS_KEY_ID"
                assert env_var["valueFrom"]["secretKeyRef"]["name"] == kfp_runtime_config.metadata["cos_secret"]
            else:
                assert env_var["value"] == kfp_runtime_config.metadata["cos_username"]
        elif env_var["name"] == "AWS_SECRET_ACCESS_KEY":
            if use_secret_for_cos_authentication:
                assert env_var["valueFrom"]["secretKeyRef"]["key"] == "AWS_SECRET_ACCESS_KEY"
                assert env_var["valueFrom"]["secretKeyRef"]["name"] == kfp_runtime_config.metadata["cos_secret"]
            else:
                assert env_var["value"] == kfp_runtime_config.metadata["cos_password"]

    # Verify that the mlpipeline specific outputs are declared
    assert node_template.get("outputs") is not None, node_template
    assert node_template["outputs"]["artifacts"] is not None, node_template["container"]["outputs"]
    assert node_template["outputs"]["artifacts"][0]["name"] == "mlpipeline-metrics"
    assert (
        node_template["outputs"]["artifacts"][0]["path"]
        == (Path(KfpPipelineProcessor.WCD) / "mlpipeline-metrics.json").as_posix()
    )
    assert node_template["outputs"]["artifacts"][1]["name"] == "mlpipeline-ui-metadata"
    assert (
        node_template["outputs"]["artifacts"][1]["path"]
        == (Path(KfpPipelineProcessor.WCD) / "mlpipeline-ui-metadata.json").as_posix()
    )
