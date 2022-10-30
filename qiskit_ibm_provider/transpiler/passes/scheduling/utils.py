# This code is part of Qiskit.
#
# (C) Copyright IBM 2022.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Utility functions for scheduling passes."""

from typing import Generator, Optional, Tuple, Union

from qiskit.circuit import Measure, Reset, Parameter
from qiskit.dagcircuit import DAGCircuit, DAGOpNode
from qiskit.transpiler.instruction_durations import (
    InstructionDurations,
    InstructionDurationsType,
)
from qiskit.transpiler.exceptions import TranspilerError


def block_order_op_nodes(dag: DAGCircuit) -> Generator[DAGOpNode, None, None]:
    """Yield nodes such that they are sorted into blocks that they minimize synchronization.

    This should be used when iterating nodes in order to find blocks within the circuit
    for IBM dynamic circuit hardware

    TODO: The need for this should be mitigated when Qiskit adds better support for
    blocks and walking them in its program representation.
    """

    def _is_grouped_measure(node: DAGOpNode) -> bool:
        """Does this node need to be grouped?"""
        return isinstance(node.op, (Reset, Measure))

    def _is_block_trigger(node: DAGOpNode) -> bool:
        """Does this node trigger the end of a block?"""
        return node.op.condition_bits

    def _emit(node: DAGOpNode) -> bool:
        """Should we emit this node?"""
        return _is_grouped_measure(node) or _is_block_trigger(node)

    # Begin processing nodes in order
    next_nodes = dag.topological_op_nodes()
    while next_nodes:
        curr_nodes = next_nodes  # Setup the next iteration nodes
        next_nodes_set = set()  # Nodes that will make it into the next iteration
        next_nodes = []  # Nodes to process in order in the next iteration
        to_push = []  # Do we push this to the very last block?
        yield_measures = []  # Measures/resets we will yield first
        yield_block_triggers = []  # Followed by block triggers (conditionals)
        block_break = False  # Did we encounter a block trigger in this iteration?
        for node in curr_nodes:
            # If we have added this node to the next set of nodes
            # skip for now.
            if node in next_nodes_set:
                next_nodes.append(node)
                continue

            # To emit nodes in the proper order
            # Fetch the descendants
            node_descendants = dag.descendants(node)
            # If this nodes is a measurement
            # push on the measurements to process
            if _is_grouped_measure(node):
                block_break = True
                next_nodes_set |= set(node_descendants)
                yield_measures.append(node)
            # If this node is a block push this onto
            # the block trigger list.
            elif _is_block_trigger(node):
                block_break = True
                next_nodes_set |= set(node_descendants)
                yield_block_triggers.append(node)
            # If this node has a descendant that is either a measurement or a block trigger
            # we will need to emit as part of this block.
            elif any(
                _emit(descendant)
                for descendant in node_descendants
                if isinstance(descendant, DAGOpNode)
            ):
                yield node
            # Otherwise we push onto the final list of blocks to emit
            # as part of the final block.
            else:
                to_push.append(node)

        # First emit the measurements which will feed
        for node in yield_measures:
            yield node
        # Into the block triggers we will emit.
        for node in yield_block_triggers:
            yield node

        # We're at the last block and emit the final nodes
        if not block_break:
            for node in to_push:
                yield node
            break
        # Otherwise emit the final nodes
        # Add to the front of the list to be processed next
        to_push.extend(next_nodes)
        next_nodes = to_push


InstrKey = Union[
    Tuple[str, None, None],
    Tuple[str, Tuple[int], None],
    Tuple[str, Tuple[int], Tuple[Parameter]],
]


class DynamicCircuitInstructionDurations(InstructionDurations):
    """For dynamic circuits the IBM Qiskit backend currently
    reports instruction durations that differ compared with those
    required for the legacy Qobj-based path. For now we use this
    class to report updated InstructionDurations.
    TODO: This would be mitigated by a specialized Backend/Target for
    dynamic circuit backends.
    """

    MEASURE_PATCH_CYCLES = 160

    def __init__(
        self,
        instruction_durations: Optional[InstructionDurationsType] = None,
        dt: float = None,
        enable_patching: bool = True,
    ):
        """Dynamic circuit instruction durations."""
        self._enable_patching = enable_patching
        super().__init__(instruction_durations=instruction_durations, dt=dt)

    def update(
        self, inst_durations: Optional[InstructionDurationsType], dt: float = None
    ) -> "DynamicCircuitInstructionDurations":
        """Update self with inst_durations (inst_durations overwrite self). Overrides the default
        durations for certain hardcoded instructions.

        Args:
            inst_durations: Instruction durations to be merged into self (overwriting self).
            dt: Sampling duration in seconds of the target backend.

        Returns:
            InstructionDurations: The updated InstructionDurations.

        Raises:
            TranspilerError: If the format of instruction_durations is invalid.
        """

        # First update as normal
        super().update(inst_durations, dt=dt)

        if not self._enable_patching or inst_durations is None:
            return self

        # Then update required instructions. This code is ugly
        # because the InstructionDurations code is handling too many
        # formats in update and this code must also.
        if isinstance(inst_durations, InstructionDurations):
            for key in inst_durations.keys():
                self._patch_instruction(key)
        else:
            for name, qubits, _, parameters, _ in inst_durations:
                if isinstance(qubits, int):
                    qubits = [qubits]

                if isinstance(parameters, (int, float)):
                    parameters = [parameters]

                if qubits is None:
                    key = (name, None, None)
                elif parameters is None:
                    key = (name, tuple(qubits), None)
                else:
                    key = (name, tuple(qubits), tuple(parameters))

                self._patch_instruction(key)

        return self

    def _patch_instruction(self, key: InstrKey) -> None:
        """Dispatcher logic for instruction patches"""
        name = key[0]
        if name == "measure":
            self._patch_measurement(key)
        elif name == "reset":
            self._patch_reset(key)

    def _patch_measurement(self, key: InstrKey) -> None:
        """Patch measurement duration by extending duration by 160dt as temporarily
        required by the dynamic circuit backend.
        """
        prev_duration, unit = self._get_duration_dt(key)
        if unit != "dt":
            raise TranspilerError('Can currently only patch durations of "dt".')
        self._patch_key(key, prev_duration + self.MEASURE_PATCH_CYCLES, unit)
        # Enforce patching of reset on measurement update
        self._patch_reset(("reset", key[1], key[2]))

    def _patch_reset(self, key: InstrKey) -> None:
        """Patch reset duration by extending duration by measurement patch as temporarily
        required by the dynamic circuit backend.
        """
        # We patch the reset to be the duration of the measurement if it
        # is available as it currently
        # triggers the end of scheduling after the measurement pulse
        measure_key = ("measure", key[1], key[2])
        try:
            measure_duration, unit = self._get_duration_dt(measure_key)
            self._patch_key(key, measure_duration, unit)
        except KeyError:
            # Fall back to reset key if measure not available
            prev_duration, unit = self._get_duration_dt(key)
            if unit != "dt":
                raise TranspilerError('Can currently only patch durations of "dt".')
            self._patch_key(key, prev_duration + self.MEASURE_PATCH_CYCLES, unit)

    def _get_duration_dt(self, key: InstrKey) -> Tuple[int, str]:
        """Handling for the complicated structure of this class.

        TODO: This class implementation should be simplified in Qiskit. Too many edge cases.
        """
        if key[1] is None and key[2] is None:
            return self.duration_by_name[key[0]]
        elif key[2] is None:
            return self.duration_by_name_qubits[(key[0], key[1])]

        return self.duration_by_name_qubits_params[key]

    def _patch_key(self, key: InstrKey, duration: int, unit: str) -> None:
        """Handling for the complicated structure of this class.

        TODO: This class implementation should be simplified in Qiskit. Too many edge cases.
        """
        if key[1] is None and key[2] is None:
            self.duration_by_name[key[0]] = (duration, unit)
        elif key[2] is None:
            self.duration_by_name_qubits[(key[0], key[1])] = (duration, unit)

        self.duration_by_name_qubits_params[key] = (duration, unit)
