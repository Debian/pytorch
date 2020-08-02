from collections import OrderedDict

from cimodel.data.pytorch_build_data import TopLevelNode, CONFIG_TREE_DATA
import cimodel.data.dimensions as dimensions
import cimodel.lib.conf_tree as conf_tree
import cimodel.lib.miniutils as miniutils
from cimodel.data.simple.util.branch_filters import gen_filter_dict
from cimodel.data.simple.util.docker_constants import gen_docker_image_path

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Conf:
    distro: str
    parms: List[str]
    parms_list_ignored_for_docker_image: Optional[List[str]] = None
    pyver: Optional[str] = None
    cuda_version: Optional[str] = None
    # TODO expand this to cover all the USE_* that we want to test for
    #  tesnrorrt, leveldb, lmdb, redis, opencv, mkldnn, ideep, etc.
    # (from https://github.com/pytorch/pytorch/pull/17323#discussion_r259453608)
    is_xla: bool = False
    vulkan: bool = False
    restrict_phases: Optional[List[str]] = None
    gpu_resource: Optional[str] = None
    dependent_tests: List = field(default_factory=list)
    parent_build: Optional['Conf'] = None
    is_libtorch: bool = False
    is_important: bool = False
    parallel_backend: Optional[str] = None

    # TODO: Eliminate the special casing for docker paths
    # In the short term, we *will* need to support special casing as docker images are merged for caffe2 and pytorch
    def get_parms(self, for_docker):
        leading = []
        # We just don't run non-important jobs on pull requests;
        # previously we also named them in a way to make it obvious
        # if self.is_important and not for_docker:
        #    leading.append("AAA")
        leading.append("pytorch")
        if self.is_xla and not for_docker:
            leading.append("xla")
        if self.is_libtorch and not for_docker:
            leading.append("libtorch")
        if self.parallel_backend is not None and not for_docker:
            leading.append(self.parallel_backend)

        cuda_parms = []
        if self.cuda_version:
            cudnn = "cudnn8" if self.cuda_version.startswith("11.") else "cudnn7"
            cuda_parms.extend(["cuda" + self.cuda_version, cudnn])
        result = leading + ["linux", self.distro] + cuda_parms + self.parms
        if not for_docker and self.parms_list_ignored_for_docker_image is not None:
            result = result + self.parms_list_ignored_for_docker_image
        return result

    def gen_docker_image_path(self):

        parms_source = self.parent_build or self
        base_build_env_name = "-".join(parms_source.get_parms(True))

        return miniutils.quote(gen_docker_image_path(base_build_env_name))

    def get_build_job_name_pieces(self, build_or_test):
        return self.get_parms(False) + [build_or_test]

    def gen_build_name(self, build_or_test):
        return ("_".join(map(str, self.get_build_job_name_pieces(build_or_test)))).replace(".", "_").replace("-", "_")

    def get_dependents(self):
        return self.dependent_tests or []

    def gen_workflow_params(self, phase):
        parameters = OrderedDict()
        build_job_name_pieces = self.get_build_job_name_pieces(phase)

        build_env_name = "-".join(map(str, build_job_name_pieces))
        parameters["build_environment"] = miniutils.quote(build_env_name)
        parameters["docker_image"] = self.gen_docker_image_path()
        if phase == "test" and self.gpu_resource:
            parameters["use_cuda_docker_runtime"] = miniutils.quote("1")
        if phase == "test":
            resource_class = "large"
            if self.gpu_resource:
                resource_class = "gpu." + self.gpu_resource
            parameters["resource_class"] = resource_class
        return parameters

    def gen_workflow_job(self, phase):
        job_def = OrderedDict()
        job_def["name"] = self.gen_build_name(phase)

        if phase == "test":

            # TODO When merging the caffe2 and pytorch jobs, it might be convenient for a while to make a
            #  caffe2 test job dependent on a pytorch build job. This way we could quickly dedup the repeated
            #  build of pytorch in the caffe2 build job, and just run the caffe2 tests off of a completed
            #  pytorch build job (from https://github.com/pytorch/pytorch/pull/17323#discussion_r259452641)

            dependency_build = self.parent_build or self
            job_def["requires"] = [dependency_build.gen_build_name("build")]
            job_name = "pytorch_linux_test"
        else:
            job_name = "pytorch_linux_build"

        if not self.is_important:
            job_def["filters"] = gen_filter_dict()
        job_def.update(self.gen_workflow_params(phase))

        return {job_name : job_def}


# TODO This is a hack to special case some configs just for the workflow list
class HiddenConf(object):
    def __init__(self, name, parent_build=None):
        self.name = name
        self.parent_build = parent_build

    def gen_workflow_job(self, phase):
        return {self.gen_build_name(phase): {"requires": [self.parent_build.gen_build_name("build")]}}

    def gen_build_name(self, _):
        return self.name


# TODO Convert these to graph nodes
def gen_dependent_configs(xenial_parent_config):

    extra_parms = [
        (["multigpu"], "large"),
        (["NO_AVX2"], "medium"),
        (["NO_AVX", "NO_AVX2"], "medium"),
        (["slow"], "medium"),
        (["nogpu"], None),
    ]

    configs = []
    for parms, gpu in extra_parms:

        c = Conf(
            xenial_parent_config.distro,
            ["py3"] + parms,
            pyver="3.6",
            cuda_version=xenial_parent_config.cuda_version,
            restrict_phases=["test"],
            gpu_resource=gpu,
            parent_build=xenial_parent_config,
            is_important=xenial_parent_config.is_important,
        )

        configs.append(c)

    return configs


def gen_docs_configs(xenial_parent_config):
    configs = []

    for x in ["pytorch_python_doc_push", "pytorch_cpp_doc_push", "pytorch_doc_test"]:
        configs.append(HiddenConf(x, parent_build=xenial_parent_config))

    return configs


def get_root():
    return TopLevelNode("PyTorch Builds", CONFIG_TREE_DATA)


def gen_tree():
    root = get_root()
    configs_list = conf_tree.dfs(root)
    return configs_list


def instantiate_configs():

    config_list = []

    root = get_root()
    found_configs = conf_tree.dfs(root)
    for fc in found_configs:

        restrict_phases = None
        distro_name = fc.find_prop("distro_name")
        compiler_name = fc.find_prop("compiler_name")
        compiler_version = fc.find_prop("compiler_version")
        is_xla = fc.find_prop("is_xla") or False
        parms_list_ignored_for_docker_image = []

        vulkan = fc.find_prop("vulkan") or False
        if vulkan:
            parms_list_ignored_for_docker_image.append("vulkan")

        python_version = None
        if compiler_name == "cuda" or compiler_name == "android":
            python_version = fc.find_prop("pyver")
            parms_list = [fc.find_prop("abbreviated_pyver")]
        else:
            parms_list = ["py" + fc.find_prop("pyver")]

        cuda_version = None
        if compiler_name == "cuda":
            cuda_version = fc.find_prop("compiler_version")

        elif compiler_name == "android":
            android_ndk_version = fc.find_prop("compiler_version")
            # TODO: do we need clang to compile host binaries like protoc?
            parms_list.append("clang5")
            parms_list.append("android-ndk-" + android_ndk_version)
            android_abi = fc.find_prop("android_abi")
            parms_list_ignored_for_docker_image.append(android_abi)
            restrict_phases = ["build"]

        elif compiler_name:
            gcc_version = compiler_name + (fc.find_prop("compiler_version") or "")
            parms_list.append(gcc_version)

            # TODO: This is a nasty special case
            if gcc_version == 'clang5' and not is_xla:
                parms_list.append("asan")
                python_version = fc.find_prop("pyver")
                parms_list[0] = fc.find_prop("abbreviated_pyver")

        if cuda_version:
            cuda_gcc_version = fc.find_prop("cuda_gcc_override") or "gcc7"
            parms_list.append(cuda_gcc_version)

        is_libtorch = fc.find_prop("is_libtorch") or False
        is_important = fc.find_prop("is_important") or False
        parallel_backend = fc.find_prop("parallel_backend") or None
        build_only = fc.find_prop("build_only") or False
        if build_only and restrict_phases is None:
            restrict_phases = ["build"]

        gpu_resource = None
        if cuda_version and cuda_version != "10":
            gpu_resource = "medium"

        c = Conf(
            distro_name,
            parms_list,
            parms_list_ignored_for_docker_image,
            python_version,
            cuda_version,
            is_xla,
            vulkan,
            restrict_phases,
            gpu_resource,
            is_libtorch=is_libtorch,
            is_important=is_important,
            parallel_backend=parallel_backend,
        )

        # run docs builds on "pytorch-linux-xenial-py3.6-gcc5.4". Docs builds
        # should run on a CPU-only build that runs on all PRs.
        if distro_name == 'xenial' and fc.find_prop("pyver") == '3.6' \
                and cuda_version is None \
                and parallel_backend is None \
                and compiler_name == 'gcc' \
                and fc.find_prop('compiler_version') == '5.4':
            c.dependent_tests = gen_docs_configs(c)

        if cuda_version == "10.1" and python_version == "3.6" and not is_libtorch:
            c.dependent_tests = gen_dependent_configs(c)

        if (compiler_name == "gcc"
                and compiler_version == "5.4"
                and not is_libtorch
                and parallel_backend is None):
            bc_breaking_check = Conf(
                "backward-compatibility-check",
                [],
                is_xla=False,
                restrict_phases=["test"],
                is_libtorch=False,
                is_important=True,
                parent_build=c,
            )
            c.dependent_tests.append(bc_breaking_check)

        config_list.append(c)

    return config_list


def get_workflow_jobs():

    config_list = instantiate_configs()

    x = []
    for conf_options in config_list:

        phases = conf_options.restrict_phases or dimensions.PHASES

        for phase in phases:

            # TODO why does this not have a test?
            if phase == "test" and conf_options.cuda_version == "10":
                continue

            x.append(conf_options.gen_workflow_job(phase))

        # TODO convert to recursion
        for conf in conf_options.get_dependents():
            x.append(conf.gen_workflow_job("test"))

    return x
