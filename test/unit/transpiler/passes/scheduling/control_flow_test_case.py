# This code is part of Qiskit.
#
# (C) Copyright IBM 2022, 2023.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

from typing import Any

from qiskit import QuantumCircuit
from qiskit.test import QiskitTestCase
from qiskit.test._canonical import canonicalize_control_flow


class ControlFlowTestCase(QiskitTestCase):

    def assertEqual(self, left: Any, right: Any):
        if isinstance(left, QuantumCircuit):
            left = canonicalize_control_flow(left)

        if isinstance(right, QuantumCircuit):
            right = canonicalize_control_flow(right)

        super().assertEqual(left, right)
