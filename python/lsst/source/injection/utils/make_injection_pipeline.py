# This file is part of source_injection.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

__all__ = ["make_injection_pipeline"]

import logging

from lsst.analysis.tools.interfaces import AnalysisPipelineTask
from lsst.pipe.base import Pipeline


def _get_dataset_type_names(conns, fields):
    """Return the name of a connection's dataset type."""
    dataset_type_names = set()
    for field in fields:
        dataset_type_names.add(getattr(conns, field).name)
    return dataset_type_names


def make_injection_pipeline(
    dataset_type_name: str,
    reference_pipeline: Pipeline | str,
    injection_pipeline: Pipeline | str | None = None,
    exclude_subsets: bool = False,
    excluded_tasks: set[str]
    | str = {
        "jointcal",
        "gbdesAstrometricFit",
        "fgcmBuildFromIsolatedStars",
        "fgcmFitCycle",
        "fgcmOutputProducts",
    },
    prefix: str = "injected_",
    instrument: str | None = None,
    log_level: int = logging.INFO,
) -> Pipeline:
    """Make an expanded source injection pipeline.

    This function takes a reference pipeline definition file in YAML format and
    prefixes all post-injection dataset type names with the injected prefix. If
    an optional injection pipeline definition YAML file is also provided, the
    injection task will be merged into the pipeline.

    Unless explicitly excluded, all subsets from the reference pipeline which
    contain the task which generates the injection dataset type will also be
    updated to include the injection task.

    Parameters
    ----------
    dataset_type_name : `str`
        Name of the dataset type being injected into.
    reference_pipeline : Pipeline | `str`
        Location of a reference pipeline definition YAML file.
    injection_pipeline : Pipeline | `str`, optional
        Location of an injection pipeline definition YAML file stub. If not
        provided, an attempt to infer the injection pipeline will be made based
        on the injected dataset type name.
    exclude_subsets : `bool`, optional
        If True, do not update pipeline subsets to include the injection task.
    excluded_tasks : `set` [`str`] | `str`
        Set or comma-separated string of task labels to exclude from the
        injection pipeline.
    prefix : `str`, optional
        Prefix to prepend to each affected post-injection dataset type name.
    instrument : `str`, optional
        Add instrument overrides. Must be a fully qualified class name.
    log_level : `int`, optional
        The log level to use for logging.

    Returns
    -------
    pipeline : `lsst.pipe.base.Pipeline`
        An expanded source injection pipeline.
    """
    # Instantiate logger.
    logger = logging.getLogger(__name__)
    logger.setLevel(log_level)

    if isinstance(reference_pipeline, str):
        pipeline = Pipeline.fromFile(reference_pipeline)
    else:
        pipeline = reference_pipeline

    # Add an instrument override, if provided.
    if instrument:
        pipeline.addInstrument(instrument)

    # Remove all tasks which are not to be included in the injection pipeline.
    if isinstance(excluded_tasks, str):
        excluded_tasks = set(excluded_tasks.split(","))
    not_excluded_tasks = set()
    for task_label in excluded_tasks:
        # First remove tasks from their host subsets, if present.
        try:
            host_subsets = pipeline.findSubsetsWithLabel(task_label)
        except ValueError:
            pass
        else:
            for host_subset in host_subsets:
                pipeline.removeLabelFromSubset(host_subset, task_label)
        # Then remove the task from the pipeline.
        try:
            pipeline.removeTask(task_label)
        except KeyError:
            not_excluded_tasks.add(task_label)
    if len(not_excluded_tasks) > 0:
        grammar = "Task" if len(not_excluded_tasks) == 1 else "Tasks"
        logger.warning(
            "%s marked for exclusion not found in the reference pipeline: %s.",
            grammar,
            ", ".join(sorted(not_excluded_tasks)),
        )

    # Determine the set of dataset type names affected by source injection.
    all_connection_type_names = set()
    injected_types = {dataset_type_name}
    precursor_injection_task_labels = set()
    # Loop over all tasks in the pipeline.
    for taskDef in pipeline.toExpandedPipeline():
        # Add override for Analysis Tools taskDefs. Connections in Analysis
        # Tools are dynamically assigned, and so are not able to be modified in
        # the same way as a static connection. Instead, we add a config
        # override here to the connections.outputName field. This field is
        # prepended to all Analysis Tools connections, and so will prepend the
        # injection prefix to all plot/metric outputs. Further processing of
        # this taskDef will be skipped thereafter.
        if issubclass(taskDef.taskClass, AnalysisPipelineTask):
            pipeline.addConfigOverride(
                taskDef.label, "connections.outputName", prefix + taskDef.config.connections.outputName
            )
            continue

        conns = taskDef.connections
        input_types = _get_dataset_type_names(conns, conns.inputs)
        output_types = _get_dataset_type_names(conns, conns.outputs)
        all_connection_type_names |= input_types | output_types
        if dataset_type_name in output_types:
            precursor_injection_task_labels.add(taskDef.label)
        # If the task has any injected dataset type names as inputs, add all of
        # its outputs to the set of injected types.
        if len(input_types & injected_types) > 0:
            injected_types |= output_types
            # Add the injection prefix to all affected dataset type names.
            for field in conns.inputs | conns.outputs:
                if hasattr(taskDef.config.connections.ConnectionsClass, field):
                    # If the connection type is not dynamic, modify as usual.
                    if (conn_type := getattr(conns, field).name) in injected_types:
                        pipeline.addConfigOverride(taskDef.label, "connections." + field, prefix + conn_type)
                else:
                    # Add log warning if the connection type is dynamic.
                    logger.warning(
                        "Dynamic connection %s in task %s is not supported here. This connection will "
                        "neither be modified nor merged into the output injection pipeline.",
                        field,
                        taskDef.label,
                    )
    # Raise if the injected dataset type does not exist in the pipeline.
    if dataset_type_name not in all_connection_type_names:
        raise RuntimeError(
            f"Dataset type '{dataset_type_name}' not found in the reference pipeline; "
            "no connection type edits to be made."
        )

    # Attempt to infer the injection pipeline from the dataset type name.
    if not injection_pipeline:
        match dataset_type_name:
            case "postISRCCD":
                injection_pipeline = "$SOURCE_INJECTION_DIR/pipelines/inject_exposure.yaml"
            case "icExp" | "calexp":
                injection_pipeline = "$SOURCE_INJECTION_DIR/pipelines/inject_visit.yaml"
            case "deepCoadd" | "deepCoadd_calexp" | "goodSeeingCoadd":
                injection_pipeline = "$SOURCE_INJECTION_DIR/pipelines/inject_coadd.yaml"
            case _:
                # Print a warning rather than a raise, as the user may wish to
                # edit connection names without merging an injection pipeline.
                logger.warning(
                    "Unable to infer injection pipeline stub from dataset type name '%s' and none was "
                    "provided. No injection pipeline will be merged into the output pipeline.",
                    dataset_type_name,
                )
        if injection_pipeline:
            logger.info(
                "Injected dataset type '%s' used to infer injection pipeline: %s",
                dataset_type_name,
                injection_pipeline,
            )

    # Merge the injection pipeline to the modified pipeline, if provided.
    if injection_pipeline:
        if isinstance(injection_pipeline, str):
            pipeline2 = Pipeline.fromFile(injection_pipeline)
        else:
            pipeline2 = injection_pipeline
        if len(pipeline2) != 1:
            raise RuntimeError(
                f"The injection pipeline contains {len(pipeline2)} tasks; only one task is allowed."
            )
        pipeline.mergePipeline(pipeline2)
        # Loop over all injection tasks and modify the connection names.
        for injection_taskDef in pipeline2.toExpandedPipeline():
            conns = injection_taskDef.connections
            pipeline.addConfigOverride(
                injection_taskDef.label, "connections.input_exposure", dataset_type_name
            )
            pipeline.addConfigOverride(
                injection_taskDef.label, "connections.output_exposure", prefix + dataset_type_name
            )
            # Optionally update subsets to include the injection task.
            if not exclude_subsets:
                for label in precursor_injection_task_labels:
                    precursor_subsets = pipeline.findSubsetsWithLabel(label)
                    for subset in precursor_subsets:
                        pipeline.addLabelToSubset(subset, injection_taskDef.label)

    logger.info("Made an injection pipeline containing %d tasks.", len(pipeline))
    return pipeline
