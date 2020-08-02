class MacOsJob:
    def __init__(self, os_version, is_test=False):
        self.os_version = os_version
        self.is_test = is_test

    def gen_tree(self):
        non_phase_parts = ["pytorch", "macos", self.os_version, "py3"]

        phase_name = "test" if self.is_test else "build"

        full_job_name = "_".join(non_phase_parts + [phase_name])

        test_build_dependency = "_".join(non_phase_parts + ["build"])
        extra_dependencies = [test_build_dependency] if self.is_test else []
        job_dependencies = extra_dependencies

        # Yes we name the job after itself, it needs a non-empty value in here
        # for the YAML output to work.
        props_dict = {"requires": job_dependencies, "name": full_job_name}

        return [{full_job_name: props_dict}]


WORKFLOW_DATA = [MacOsJob("10_13"), MacOsJob("10_13", True)]


def get_workflow_jobs():
    return [item.gen_tree() for item in WORKFLOW_DATA]
