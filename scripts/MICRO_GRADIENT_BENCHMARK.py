#!/usr/bin/env python3
"""
===============================================================================
MICRO-GRADIENT SCALING MATRIX (N=3 to 10)
Focused 3-Way Quantum Compiler Benchmark: Entanglement Topology & Coherence
===============================================================================
"""

from __future__ import annotations

import os
import sys
import time
import json
import random
import math
import hashlib
import platform
import traceback
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

try:
    import h5py
    HAS_HDF5 = True
except ImportError:
    HAS_HDF5 = False
    print("[WARNING] h5py not found. .hdf5 telemetry export will be skipped.")

# ---------------------------------------------------------------------------
# Qiskit
# ---------------------------------------------------------------------------
import qiskit
from qiskit import QuantumCircuit, transpile, qasm2
from qiskit.quantum_info import Statevector
from qiskit.converters import circuit_to_dag, dag_to_circuit
from qiskit.transpiler import PassManager
from qiskit.transpiler.passes import BasisTranslator, UnrollCustomDefinitions
from qiskit.circuit.equivalence_library import SessionEquivalenceLibrary as sel
import qiskit_ibm_runtime
from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2 as Sampler

# ---------------------------------------------------------------------------
# Optional TKET
# ---------------------------------------------------------------------------
try:
    import pytket
    from pytket.extensions.qiskit import qiskit_to_tk, tk_to_qiskit
    from pytket.circuit import Node
    from pytket.architecture import Architecture
    from pytket.passes import FullPeepholeOptimise, RoutingPass
    from pytket.placement import LinePlacement
    import re
    HAS_TKET = True
except Exception as e:
    print(f"\n[FATAL IMPORT ERROR - TKET]: {e}")
    pytket = None
    HAS_TKET = False

# ---------------------------------------------------------------------------
# Prometheus (Reverted to V15 per Analytics)
# ---------------------------------------------------------------------------
try:
    from prometheus_v15 import optimize as prometheus_optimize
    PROMETHEUS_VERSION = "15.x-local"
    HAS_PROMETHEUS = True
except Exception:
    prometheus_optimize = None
    PROMETHEUS_VERSION = "NOT_AVAILABLE"
    HAS_PROMETHEUS = False

# =============================================================================
# CONFIGURATION
# =============================================================================

BACKEND_NAME = os.environ.get("CRUCIBLE_BACKEND", "ibm_marrakesh")
SHOTS = int(os.environ.get("CRUCIBLE_SHOTS", "10000"))
SEED = int(os.environ.get("CRUCIBLE_SEED", "20260814"))
SEMANTIC_THRESHOLD = float(os.environ.get("CRUCIBLE_SEMANTIC_THRESHOLD", "0.99"))
BOOTSTRAPS = int(os.environ.get("CRUCIBLE_BOOTSTRAPS", "500"))

COMPILERS = ("SABRE_O3", "TKET", "PROMETHEUS")
UNIFIED_SINGLE_JOB = True

# =============================================================================
# DATA MODEL
# =============================================================================

@dataclass
class RunRecord:
    benchmark: str
    compiler: str
    status: str = "UNSET"
    compile_time_sec: float = 0.0
    mapping_source: str = ""
    routing_topology: str = ""
    logical_to_physical_map: Optional[List[int]] = None
    source_wire_to_physical_map: Optional[Dict[str, int]] = None
    semantic_fidelity: Optional[float] = None
    hashes: Optional[Dict[str, str]] = None
    routed_metrics: Optional[Dict[str, Any]] = None
    final_executable_metrics: Optional[Dict[str, Any]] = None
    edge_sets: Optional[Dict[str, Any]] = None
    hardware_metrics: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    legalized_circuit_obj: Any = None

# =============================================================================
# ARTIFACT/HASH UTILITIES
# =============================================================================

def hash_data(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()

def hash_circuit(circuit: QuantumCircuit) -> Tuple[str, str]:
    try:
        text = qasm2.dumps(circuit)
    except Exception:
        from qiskit import qasm3
        text = qasm3.dumps(circuit)
    return hash_data(text), text

def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

def sanitize_keys(obj):
    if isinstance(obj, dict):
        return {str(k) if isinstance(k, tuple) else k: sanitize_keys(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_keys(item) for item in obj]
    return obj

def serialize_target(backend, out_dir: Path) -> str:
    rows = []
    for inst_name, qarg_props in backend.target.items():
        for qargs, props in qarg_props.items():
            rows.append({
                "operation": str(inst_name),
                "qargs": None if qargs is None else list(qargs),
                "duration": getattr(props, "duration", None),
                "error": getattr(props, "error", None),
            })
    rows.sort(key=lambda x: (x["operation"], str(x["qargs"])))
    text = json.dumps(rows, indent=2, sort_keys=True)
    write_text(out_dir / "target_metadata.json", text)
    return hash_data(text)

def serialize_environment(backend, out_dir: Path) -> str:
    env = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "qiskit": qiskit.__version__,
        "qiskit_ibm_runtime": qiskit_ibm_runtime.__version__,
        "pytket": getattr(pytket, "__version__", None) if HAS_TKET else None,
        "prometheus": PROMETHEUS_VERSION,
        "backend": BACKEND_NAME,
        "backend_num_qubits": backend.num_qubits,
        "backend_calibration_timestamp": (
            str(backend.properties().last_update_date)
            if backend.properties() is not None else None
        ),
        "prometheus_model_dir": os.environ.get("PROMETHEUS_MODEL_DIR"),
    }
    text = json.dumps(env, indent=2, sort_keys=True)
    write_text(out_dir / "env.json", text)
    return hash_data(text)

# =============================================================================
# BENCHMARK CIRCUITS (FOCUSED MICRO-GRADIENT)
# =============================================================================

def generate_ghz(n: int) -> QuantumCircuit:
    qc = QuantumCircuit(n, n, name=f"GHZ_{n}")
    qc.h(0)
    for i in range(n - 1):
        qc.cx(i, i + 1)
    qc.measure(range(n), range(n))
    return qc

def generate_qft_nontrivial(n: int) -> QuantumCircuit:
    qc = QuantumCircuit(n, n, name=f"QFT_{n}_NONTRIVIAL")
    qc.x(0)
    if n > 3: qc.x(3)
    if n > 5: qc.x(5)
    for i in range(n):
        qc.h(i)
        for j in range(i + 1, n):
            qc.cp(math.pi / (2 ** (j - i)), j, i)
    for i in range(n // 2):
        qc.swap(i, n - 1 - i)
    qc.measure(range(n), range(n))
    return qc

def generate_cross_entanglement(n: int) -> QuantumCircuit:
    qc = QuantumCircuit(n, n, name=f"CrossEnt_{n}")
    qc.h(range(n))
    for i in range(n - 1): 
        qc.cx(i, i + 1)
    if n > 2:
        for i in range(n - 2): 
            qc.cx(i, i + 2)
    qc.rx(math.pi/4, range(n))
    qc.measure(range(n), range(n))
    return qc

def generate_qaoa_maxcut(n: int) -> QuantumCircuit:
    edges = [(i, (i + 1) % n) for i in range(n)]
    for i in range(n // 2): 
        edges.append((i, (i + n // 2) % n))
    edges = list(set([tuple(sorted(e)) for e in edges]))
    
    qc = QuantumCircuit(n, n, name=f"QAOA_{n}_MAXCUT")
    qc.h(range(n))
    gamma, beta = 0.392, 0.785
    for u, v in edges: 
        qc.rzz(2 * gamma, u, v)
    for i in range(n): 
        qc.rx(2 * beta, i)
    qc.measure(range(n), range(n))
    return qc

# ---------------------------------------------------------------------------
# MICRO-GRADIENT FAMILIES MAP
# ---------------------------------------------------------------------------
GRADIENT_SIZES = [3, 4, 5, 6, 7, 8, 9, 10]

FAMILIES = {
    "GHZ": (GRADIENT_SIZES, generate_ghz),
    "QFT_NON": (GRADIENT_SIZES, generate_qft_nontrivial),
    "CrossEnt": (GRADIENT_SIZES, generate_cross_entanglement),
    "QAOA": (GRADIENT_SIZES, generate_qaoa_maxcut),
}

# =============================================================================
# EXACT DISTRIBUTION / SEMANTIC CHECKING
# =============================================================================

def extract_measured_distribution(circuit: QuantumCircuit) -> Dict[str, float]:
    if circuit.num_clbits == 0:
        return {}
    q_index = {q: circuit.find_bit(q).index for q in circuit.qubits}
    meas_map: Dict[int, int] = {}
    for inst in circuit.data:
        if inst.operation.name == "measure":
            q_idx = q_index[inst.qubits[0]]
            c_idx = circuit.find_bit(inst.clbits[0]).index
            meas_map[q_idx] = c_idx
    dag = circuit_to_dag(circuit)
    active_qubits = [q for q in dag.qubits if dag.nodes_on_wire(q, only_ops=True) or q_index[q] in meas_map]
    active_indices = [q_index[q] for q in active_qubits]
    for q in list(dag.qubits):
        if q not in active_qubits:
            dag.remove_qubits(q)
    dag.remove_all_ops_named("measure")
    dag.remove_all_ops_named("barrier")
    dag.remove_all_ops_named("delay")
    active = dag_to_circuit(dag)
    sv = Statevector(active)
    probs = sv.probabilities_dict()
    out: Dict[str, float] = {}
    ncl = circuit.num_clbits
    for bitstr, p in probs.items():
        if p < 1e-15:
            continue
        classical = ["0"] * ncl
        for active_idx, val in enumerate(reversed(bitstr)):
            phys_q = active_indices[active_idx]
            if phys_q in meas_map:
                c_idx = meas_map[phys_q]
                classical[ncl - 1 - c_idx] = val
        key = "".join(classical)
        out[key] = out.get(key, 0.0) + float(p)
    return out

def semantic_fidelity(
    logical_circuit: QuantumCircuit,
    canonical_physical_circuit: QuantumCircuit,
    logical_to_physical: List[int],
    ideal_dist: Dict[str, float],
) -> float:
    validate_mapping_contract(logical_circuit, canonical_physical_circuit, logical_to_physical)

    if logical_circuit.num_qubits > 16:
        return None

    dag = circuit_to_dag(canonical_physical_circuit)
    original_q_index = {q: canonical_physical_circuit.find_bit(q).index for q in dag.qubits}

    dag.remove_all_ops_named("measure")
    dag.remove_all_ops_named("barrier")
    dag.remove_all_ops_named("delay")

    active_qubits = [q for q in dag.qubits if dag.nodes_on_wire(q, only_ops=True)]
    active_physical_indices = [original_q_index[q] for q in active_qubits]

    if len(active_qubits) > 16:
        return None

    for q in list(dag.qubits):
        if q not in active_qubits:
            dag.remove_qubits(q)

    active = dag_to_circuit(dag)
    if active.num_qubits == 0:
        return 1.0 if not ideal_dist else 0.0

    sv = Statevector(active)
    probs = sv.probabilities_dict()

    physical_to_logical = {p: i for i, p in enumerate(logical_to_physical)}
    logical_probs: Dict[str, float] = {}

    for bitstr, probability in probs.items():
        if probability < 1e-15:
            continue
        logical_bits = ["0"] * logical_circuit.num_qubits
        for active_idx, bit_val in enumerate(reversed(bitstr)):
            pidx = active_physical_indices[active_idx]
            if pidx in physical_to_logical:
                lidx = physical_to_logical[pidx]
                logical_bits[logical_circuit.num_qubits - 1 - lidx] = bit_val
        key = "".join(logical_bits)
        logical_probs[key] = logical_probs.get(key, 0.0) + float(probability)

    keys = set(ideal_dist) | set(logical_probs)
    bc = sum(math.sqrt(ideal_dist.get(k, 0.0) * logical_probs.get(k, 0.0)) for k in keys)
    return round(bc * bc, 6)


# =============================================================================
# TARGET / TOPOLOGY CONTRACTS
# =============================================================================

def target_edges(backend) -> set[Tuple[int, int]]:
    edges = set()
    if getattr(backend, "coupling_map", None):
        for q0, q1 in backend.coupling_map.get_edges():
            edges.add(tuple(sorted((q0, q1))))
    for _, qargs in backend.target.instructions:
        if qargs is not None and len(qargs) == 2:
            edges.add(tuple(sorted(qargs)))
    return edges

def extract_physical_edge_multiset(circuit: QuantumCircuit) -> Counter[Tuple[int, int]]:
    edges: Counter = Counter()
    for inst in circuit.data:
        if len(inst.qubits) == 2:
            q0 = circuit.find_bit(inst.qubits[0]).index
            q1 = circuit.find_bit(inst.qubits[1]).index
            edges[tuple(sorted((q0, q1)))] += 1
    return edges

def validate_mapping_contract(logical_circuit: QuantumCircuit, canonical_circuit: QuantumCircuit, logical_to_physical: Optional[List[int]]) -> None:
    if logical_to_physical is None:
        raise RuntimeError("Missing logical_to_physical placement.")
    if len(logical_to_physical) != logical_circuit.num_qubits:
        raise RuntimeError("Logical-to-physical map length does not equal logical qubit count.")
    if len(set(logical_to_physical)) != len(logical_to_physical):
        raise RuntimeError("Logical-to-physical map is not injective.")
    if not all(0 <= p < canonical_circuit.num_qubits for p in logical_to_physical):
        raise RuntimeError("Logical-to-physical map contains invalid coordinates.")

def validate_routing_contract(circuit: QuantumCircuit, backend) -> None:
    valid_edges = target_edges(backend)
    for inst in circuit.data:
        if len(inst.qubits) == 2:
            q0 = circuit.find_bit(inst.qubits[0]).index
            q1 = circuit.find_bit(inst.qubits[1]).index
            if tuple(sorted((q0, q1))) not in valid_edges:
                raise RuntimeError(f"Routing contract failed: {inst.operation.name}{q0,q1} uses an unsupported edge.")

def validate_native_contract(circuit: QuantumCircuit, backend) -> None:
    ignored = {"barrier", "delay", "measure"}
    for inst in circuit.data:
        if inst.operation.name in ignored:
            continue
        qargs = tuple(circuit.find_bit(q).index for q in inst.qubits)
        if not backend.target.instruction_supported(inst.operation.name, qargs):
            raise RuntimeError(f"Native contract failed: {inst.operation.name}{qargs} unsupported by target.")

def validate_canonical_width(circuit: QuantumCircuit, backend) -> None:
    if circuit.num_qubits != backend.num_qubits:
        raise RuntimeError(f"Canonical width mismatch: {circuit.num_qubits} != {backend.num_qubits}")

# =============================================================================
# CANONICAL CIRCUIT ADAPTER
# =============================================================================

def build_canonical_circuit(routed_temp: QuantumCircuit, source_qubit_to_physical: Dict[Any, int], backend, num_clbits: int) -> QuantumCircuit:
    if set(source_qubit_to_physical.keys()) != set(routed_temp.qubits):
        missing = set(routed_temp.qubits) - set(source_qubit_to_physical.keys())
        extra = set(source_qubit_to_physical.keys()) - set(routed_temp.qubits)
        raise RuntimeError(f"Source->physical map mismatch. missing={missing}, extra={extra}")
    physical = list(source_qubit_to_physical.values())
    if len(set(physical)) != len(physical):
        raise RuntimeError("Source->physical map is not injective.")
    if not all(0 <= p < backend.num_qubits for p in physical):
        raise RuntimeError("Source->physical map has invalid physical index.")

    canonical = QuantumCircuit(backend.num_qubits, num_clbits, name=routed_temp.name)
    q_map = {src_q: canonical.qubits[phys_idx] for src_q, phys_idx in source_qubit_to_physical.items()}
    c_index = {src_c: i for i, src_c in enumerate(routed_temp.clbits)}

    for inst in routed_temp.data:
        qargs = [q_map[q] for q in inst.qubits]
        cargs = [canonical.clbits[c_index[c]] for c in inst.clbits]
        canonical.append(inst.operation, qargs, cargs)

    validate_canonical_width(canonical, backend)
    return canonical

# =============================================================================
# COMPILERS
# =============================================================================

def compile_sabre(logical_circuit: QuantumCircuit, backend) -> Dict[str, Any]:
    start = time.perf_counter()
    routed = transpile(logical_circuit, target=backend.target, optimization_level=3, routing_method="sabre", layout_method="sabre", seed_transpiler=SEED)
    final_map = list(routed.layout.final_index_layout(filter_ancillas=True))
    source_map = {q: routed.find_bit(q).index for q in routed.qubits}
    return {"circuit": routed, "logical_to_physical": final_map, "source_to_physical": source_map, "mapping_source": "qiskit_layout", "routing_topology": "SABRE O3", "compile_time_sec": time.perf_counter() - start}

def compile_tket(logical_circuit: QuantumCircuit, backend) -> Dict[str, Any]:
    if not HAS_TKET: raise RuntimeError("pytket is not installed.")
    start = time.perf_counter()
    tk_logical = transpile(logical_circuit, basis_gates=['cx', 'id', 'rz', 'sx', 'x'], optimization_level=1)
    tk_circ = qiskit_to_tk(tk_logical)
    logical_qubits = list(tk_circ.qubits)
    edges = backend.coupling_map.get_edges() if getattr(backend, "coupling_map", None) else []
    architecture = Architecture(edges)
    FullPeepholeOptimise().apply(tk_circ)
    placement = LinePlacement(architecture)
    placement_map = placement.get_placement_map(tk_circ)
    placement.place(tk_circ)
    RoutingPass(architecture).apply(tk_circ)
    permutation = tk_circ.implicit_qubit_permutation()
    final_map = []
    for lq in logical_qubits:
        initial_node = placement_map[lq]
        final_node = permutation.get(initial_node, initial_node)
        final_map.append(int(final_node.index[0]))
    routed_qiskit = tk_to_qiskit(tk_circ)
    source_map = {}
    for q in routed_qiskit.qubits:
        m = re.search(r'index=(\d+)', repr(q))
        if m: source_map[q] = int(m.group(1))
        else: source_map[q] = int("".join(ch for ch in str(q) if ch.isdigit()))
    canonical = build_canonical_circuit(routed_qiskit, source_map, backend, logical_circuit.num_clbits)
    return {"circuit": canonical, "logical_to_physical": final_map, "source_to_physical": source_map, "mapping_source": "TKET LinePlacement", "routing_topology": "TKET Arch", "compile_time_sec": time.perf_counter() - start}

def compile_prometheus(logical_circuit: QuantumCircuit, backend) -> Dict[str, Any]:
    if not HAS_PROMETHEUS: raise RuntimeError("Prometheus is not installed.")
    start = time.perf_counter()
    result = prometheus_optimize(logical_circuit.copy(), backend=backend, return_mapping=True)
    routed_temp = result.get("circuit")
    logical_map = result.get("logical_to_physical_final")
    source_map = result.get("source_qubit_to_physical_map")
    canonical = build_canonical_circuit(routed_temp, source_map, backend, logical_circuit.num_clbits)
    return {"circuit": canonical, "logical_to_physical": [int(p) for p in logical_map], "source_to_physical": source_map, "mapping_source": "Prometheus", "routing_topology": "Prometheus", "compile_time_sec": time.perf_counter() - start}

COMPILER_FUNCS = {
    "SABRE_O3": compile_sabre,
    "TKET": compile_tket,
    "PROMETHEUS": compile_prometheus,
}

# =============================================================================
# COMMON POST-ROUTING TRANSLATION
# =============================================================================

def common_hardware_translate(routed_canonical: QuantumCircuit, backend) -> QuantumCircuit:
    pm = PassManager([
        UnrollCustomDefinitions(sel, target=backend.target),
        BasisTranslator(sel, list(backend.target.operation_names), target=backend.target),
    ])
    translated = pm.run(routed_canonical)
    translated.name = routed_canonical.name
    return translated

# =============================================================================
# METRICS
# =============================================================================

def circuit_metrics(circuit: QuantumCircuit, backend=None) -> Dict[str, Any]:
    ignored = {"measure", "barrier", "delay"}
    one_q = 0
    two_q_total = 0
    native_two_q = 0
    gate_count = 0
    active_qubits = set()

    for inst in circuit.data:
        if inst.operation.name in ignored:
            continue
        gate_count += 1
        for q in inst.qubits:
            active_qubits.add(circuit.find_bit(q).index)
        nq = len(inst.qubits)
        if nq == 1:
            one_q += 1
        elif nq == 2:
            two_q_total += 1
            if backend is not None:
                qargs = tuple(circuit.find_bit(q).index for q in inst.qubits)
                if backend.target.instruction_supported(inst.operation.name, qargs):
                    native_two_q += 1

    two_q_depth = circuit.depth(filter_function=lambda x: len(x.qubits) > 1 and x.operation.name not in ignored)
    one_q_depth = circuit.depth(filter_function=lambda x: len(x.qubits) == 1 and x.operation.name not in ignored)

    return {
        "gate_count": gate_count,
        "1q_gates": one_q,
        "abstract_2q_operations": two_q_total,
        "native_2q_operations": native_two_q if backend is not None else None,
        "active_physical_qubit_count": len(active_qubits),
        "active_physical_qubits": sorted(active_qubits),
        "unique_2q_edge_count": len(extract_physical_edge_multiset(circuit)),
        "depth": circuit.depth(),
        "one_qubit_depth": one_q_depth,
        "two_qubit_depth": two_q_depth,
        "scheduled_duration_dt": getattr(circuit, "duration", None),
    }

def kl_divergence(P: Dict[str, float], Q: Dict[str, float], keys) -> float:
    total = 0.0
    for k in keys:
        p, q = P.get(k, 0.0), Q.get(k, 0.0)
        if p == 0.0: continue
        if q == 0.0: return float("inf")
        total += p * math.log2(p / q)
    return total

def distribution_metrics(counts: Dict[str, int], ideal: Dict[str, float], shots: int) -> Dict[str, Any]:
    empirical = {k: v / shots for k, v in counts.items() if v > 0}
    keys = set(empirical) | set(ideal)
    entropy = -sum(p * math.log2(p) for p in empirical.values() if p > 0)
    sum_p2 = sum(p * p for p in empirical.values())
    renyi2 = -math.log2(sum_p2) if sum_p2 > 0 else 0.0
    max_p = max(empirical.values()) if empirical else 0.0
    min_entropy = -math.log2(max_p) if max_p > 0 else 0.0
    tvd = 0.5 * sum(abs(empirical.get(k, 0.0) - ideal.get(k, 0.0)) for k in keys)
    bc = sum(math.sqrt(empirical.get(k, 0.0) * ideal.get(k, 0.0)) for k in keys)
    hellinger_fidelity = bc * bc
    hellinger_distance = math.sqrt(max(0.0, 1.0 - bc))
    M = {k: 0.5 * (empirical.get(k, 0.0) + ideal.get(k, 0.0)) for k in keys}
    jsd = 0.5 * kl_divergence(empirical, M, keys) + 0.5 * kl_divergence(ideal, M, keys)
    
    all_ideal = list(ideal.values())
    median = float(np.median(all_ideal)) if all_ideal else 0.0
    if median == 0.0:
        hop = {"value": None, "status": "UNDEFINED", "reason": "ideal_distribution_median_zero"}
    else:
        heavy = {k for k, p in ideal.items() if p > median}
        hop = {"value": round(sum(empirical.get(k, 0.0) for k in heavy), 6), "status": "VALID"}
    return {
        "Shannon_entropy": round(entropy, 6),
        "Renyi2_entropy": round(renyi2, 6),
        "min_entropy": round(min_entropy, 6),
        "TVD": round(tvd, 6),
        "Hellinger_distance": round(hellinger_distance, 6),
        "Hellinger_fidelity": round(hellinger_fidelity, 6),
        "JSD": round(jsd, 6),
        "heavy_output_probability": hop,
    }

def bootstrap_uncertainty(counts: Dict[str, int], ideal: Dict[str, float], shots: int, n_boot: int = BOOTSTRAPS) -> Dict[str, float]:
    keys = list(counts)
    probs = [counts[k] / shots for k in keys]
    fh, tvd, jsd = [], [], []
    
    M_base = {k: 0.5 * ideal.get(k, 0.0) for k in ideal}
    
    for _ in range(n_boot):
        sample = np.random.multinomial(shots, probs)
        boot_counts = {keys[i]: int(sample[i]) for i in range(len(keys)) if sample[i] > 0}
        
        empirical = {k: v / shots for k, v in boot_counts.items() if v > 0}
        eval_keys = set(empirical) | set(ideal)
        
        bc = sum(math.sqrt(empirical.get(k, 0.0) * ideal.get(k, 0.0)) for k in eval_keys)
        fh.append(bc * bc)
        
        tvd.append(0.5 * sum(abs(empirical.get(k, 0.0) - ideal.get(k, 0.0)) for k in eval_keys))
        
        M = {k: 0.5 * empirical.get(k, 0.0) + M_base.get(k, 0.0) for k in eval_keys}
        jsd.append(0.5 * kl_divergence(empirical, M, eval_keys) + 0.5 * kl_divergence(ideal, M, eval_keys))

    return {
        "Hellinger_fidelity_std": round(float(np.std(fh)), 6),
        "TVD_std": round(float(np.std(tvd)), 6),
        "JSD_std": round(float(np.std(jsd)), 6),
    }

def binomial_margin_95(p: float, n: int) -> float:
    if n <= 0: return 0.0
    return round(1.96 * math.sqrt(max(0.0, p * (1.0 - p)) / n), 6)

def build_algorithm_metrics(benchmark_name: str, counts: Dict[str, int]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if benchmark_name.startswith("GHZ_"):
        n = int(benchmark_name.split("_")[1])
        p = (counts.get("0" * n, 0) + counts.get("1" * n, 0)) / SHOTS
        out["algorithmic_success"] = round(p, 6)
        out["algorithmic_success_margin_95"] = binomial_margin_95(p, SHOTS)
    return out

# =============================================================================
# ARTIFACT DIRECTORY
# =============================================================================

def make_run_directory() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    root = Path(f"crucible_run_{stamp}")

    for sub in (
        "logical",
        "routed",
        "translated",
        "environment",
        "target",
        "results",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)

    return root

# =============================================================================
# EXECUTION & RESULTS
# =============================================================================

def compile_arm(
    compiler_name: str,
    benchmarks: Dict[str, QuantumCircuit],
    ideals: Dict[str, Dict[str, float]],
    backend,
    root: Path,
) -> List[RunRecord]:
    fn = COMPILER_FUNCS[compiler_name]
    records: List[RunRecord] = []

    for benchmark_name, logical in benchmarks.items():
        print(f"    {compiler_name:12s} -> {benchmark_name}")
        try:
            compiled = fn(logical, backend)
            canonical = compiled["circuit"]
            canonical.name = f"{benchmark_name}__{compiler_name}"

            validate_canonical_width(canonical, backend)
            logical_map = compiled["logical_to_physical"]
            validate_mapping_contract(logical, canonical, logical_map)
            validate_routing_contract(canonical, backend)

            sem_fid = semantic_fidelity(
                logical, canonical, logical_map, ideals[benchmark_name]
            )
            
            if sem_fid is None:
                record_status = "COMPILED_UNVERIFIED"
            elif sem_fid < SEMANTIC_THRESHOLD:
                raise RuntimeError(
                    f"Semantic fidelity {sem_fid:.6f} < threshold {SEMANTIC_THRESHOLD:.6f}."
                )
            else:
                record_status = "COMPILED_SEMANTIC_PASS"

            routed_metrics = circuit_metrics(canonical)
            logical_metrics = circuit_metrics(logical)
            routed_edges = extract_physical_edge_multiset(canonical)
            r_hash, r_qasm = hash_circuit(canonical)
            write_text(root / "routed" / f"{benchmark_name}__{compiler_name}.qasm", r_qasm)

            translated = common_hardware_translate(canonical, backend)
            translated.name = canonical.name
            translated_edges = extract_physical_edge_multiset(translated)

            if not set(translated_edges).issubset(set(routed_edges)):
                raise RuntimeError(
                    "Common translation introduced a new physical two-qubit edge."
                )
            validate_native_contract(translated, backend)

            t_hash, t_qasm = hash_circuit(translated)
            write_text(root / "translated" / f"{benchmark_name}__{compiler_name}.qasm", t_qasm)

            final_metrics = circuit_metrics(translated, backend)
            logical_2q = logical_metrics["abstract_2q_operations"]
            routed_2q = routed_metrics["abstract_2q_operations"]
            final_native_2q = final_metrics["native_2q_operations"]
            e_route = routed_2q / max(1, logical_2q)
            e_translate = final_native_2q / max(1, routed_2q) if final_native_2q is not None else None

            records.append(RunRecord(
                benchmark=benchmark_name,
                compiler=compiler_name,
                status=record_status,
                compile_time_sec=round(compiled["compile_time_sec"], 6),
                mapping_source=compiled["mapping_source"],
                routing_topology=compiled["routing_topology"],
                logical_to_physical_map=[int(x) for x in logical_map],
                source_wire_to_physical_map={
                    str(k): int(v) for k, v in compiled["source_to_physical"].items()
                },
                semantic_fidelity=sem_fid,
                hashes={
                    "routed": r_hash,
                    "translated": t_hash,
                },
                routed_metrics=routed_metrics,
                final_executable_metrics={
                    "logical_gate_count": logical_metrics["gate_count"],
                    "logical_1q_gates": logical_metrics["1q_gates"],
                    "logical_abstract_2q_gates": logical_2q,
                    "logical_depth": logical_metrics["depth"],
                    "routed_gate_count": routed_metrics["gate_count"],
                    "routed_1q_gates": routed_metrics["1q_gates"],
                    "routed_abstract_2q_gates": routed_2q,
                    "routing_induced_2q_overhead": routed_2q - logical_2q,
                    "unique_physical_edges_used": routed_metrics["unique_2q_edge_count"],
                    "final_native_2q_operations": final_native_2q,
                    "translated_1q_gates": final_metrics["1q_gates"],
                    "E_route_abstract": round(e_route, 6),
                    "E_translate": round(e_translate, 6) if e_translate is not None else None,
                    "total_2q_expansion": round(final_native_2q / max(1, logical_2q), 6) if final_native_2q is not None else None,
                    "depth": final_metrics["depth"],
                    "two_qubit_depth": final_metrics["two_qubit_depth"],
                    "scheduled_duration_dt": final_metrics["scheduled_duration_dt"],
                },
                edge_sets={
                    "routed_physical_edges_counts": dict(routed_edges),
                    "translated_physical_edges_counts": dict(translated_edges),
                },
                legalized_circuit_obj=translated,
            ))
        except Exception as exc:
            records.append(RunRecord(
                benchmark=benchmark_name,
                compiler=compiler_name,
                status="COMPILE_OR_VALIDATION_FAIL",
                error="".join(traceback.format_exception_only(type(exc), exc)).strip(),
            ))
            print(f"        [FAIL] {compiler_name} / {benchmark_name}: {exc}")

    return records


def decode_counts(pub_result) -> Dict[str, int]:
    if hasattr(pub_result.data, "meas"):
        return pub_result.data.meas.get_counts()
    if hasattr(pub_result.data, "c"):
        return pub_result.data.c.get_counts()
    raise RuntimeError("Unable to locate measurement BitArray in SamplerV2 result.")


def submit_unified_job(records: List[RunRecord], backend):
    expected = len(BENCHMARKS) * len(COMPILERS)
    
    valid_statuses = {"COMPILED_SEMANTIC_PASS", "COMPILED_UNVERIFIED"}
    valid = [r for r in records if r.status in valid_statuses and r.legalized_circuit_obj is not None]

    if len(valid) != expected:
        failures = [
            {"benchmark": r.benchmark, "compiler": r.compiler, "status": r.status, "error": r.error}
            for r in records if r.status not in valid_statuses
        ]
        raise RuntimeError(
            f"Fatal pre-QPU matrix failure: {len(valid)}/{expected} passed.\n"
            + json.dumps(failures, indent=2)
        )

    rng = random.Random(SEED)
    rng.shuffle(valid)
    payload = [r.legalized_circuit_obj for r in valid]
    execution_order = [f"{i}: {r.compiler} / {r.benchmark}" for i, r in enumerate(valid)]

    sampler = Sampler(mode=backend)
    sampler_control = {
        "shots_per_pub": SHOTS,
        "dynamical_decoupling": {"enable": False},
        "twirling": {"enable_gates": False, "enable_measure": False},
    }
    sampler.options.dynamical_decoupling.enable = False
    sampler.options.twirling.enable_gates = False
    sampler.options.twirling.enable_measure = False

    job = sampler.run(payload, shots=SHOTS)
    return job, valid, execution_order, sampler_control


def process_unified_result(job, valid_records, ideals, root: Path):
    results = job.result()
    raw_counts = {}
    summary_rows = []

    for idx, record in enumerate(valid_records):
        counts = decode_counts(results[idx])
        key = f"{record.benchmark}__{record.compiler}"
        raw_counts[key] = counts

        metrics = distribution_metrics(counts, ideals[record.benchmark], SHOTS)
        bootstrap = bootstrap_uncertainty(counts, ideals[record.benchmark], SHOTS)
        algo = build_algorithm_metrics(record.benchmark, counts)

        record.hardware_metrics = {
            **metrics,
            **algo,
            "bootstrap_uncertainty": bootstrap,
            "execution_index": idx,
        }

        row = {
            "execution_index": idx,
            "benchmark": record.benchmark,
            "compiler": record.compiler,
            "status": record.status,
            "compile_time_sec": record.compile_time_sec,
            "semantic_fidelity": record.semantic_fidelity,
            **(record.routed_metrics or {}),
            **(record.final_executable_metrics or {}),
            **record.hardware_metrics,
        }
        summary_rows.append(row)

    # Standard JSON Exports
    write_text(root / "results" / "raw_counts.json", json.dumps(raw_counts, indent=2, sort_keys=True))
    write_text(root / "results" / "summary.json", json.dumps(summary_rows, indent=2, default=str))

    import csv
    flat_path = root / "results" / "summary.csv"
    fieldnames = sorted({k for row in summary_rows for k in row.keys()})
    with flat_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary_rows)

    # Extensive .hdf5 Telemetry Export
    if HAS_HDF5:
        hdf5_path = root / "results" / "telemetry.hdf5"
        with h5py.File(hdf5_path, "w") as f:
            counts_grp = f.create_group("raw_counts")
            for key, counts in raw_counts.items():
                ds = counts_grp.create_dataset(key, data=list(counts.values()))
                ds.attrs["keys"] = json.dumps(list(counts.keys()))
                
            metrics_grp = f.create_group("metrics")
            for row in summary_rows:
                row_grp = metrics_grp.create_group(f"{row['benchmark']}__{row['compiler']}")
                for k, v in row.items():
                    if isinstance(v, (int, float, str)):
                        row_grp.attrs[k] = v
                    elif v is None:
                        row_grp.attrs[k] = "None"
        print(f"    -> Extensive telemetry written to {hdf5_path.name}")

    return raw_counts, summary_rows


def main():
    global BENCHMARKS

    print("=" * 90)
    print("CRUCIBLE SCALING MATRIX — 3-WAY UNIFIED COMPILER BENCHMARK (N=3 TO 10 MICRO-GRADIENT)")
    print("=" * 90)
    print(f"Backend: {BACKEND_NAME}")
    print(f"Shots/PUB: {SHOTS}")
    print(f"Bootstraps: {BOOTSTRAPS}")
    print(f"Compilers: {', '.join(COMPILERS)}")
    print()

    root = make_run_directory()
    service = QiskitRuntimeService(channel="ibm_quantum_platform")
    backend = service.backend(BACKEND_NAME)

    target_hash = serialize_target(backend, root / "target")
    environment_hash = serialize_environment(backend, root / "environment")

    BENCHMARKS = {}
    ideals = {}

    for family, (sizes, generator) in FAMILIES.items():
        for size in sizes:
            circuit = generator(size)
            BENCHMARKS[circuit.name] = circuit
            ideals[circuit.name] = extract_measured_distribution(circuit)
            _, qasm = hash_circuit(circuit)
            write_text(root / "logical" / f"{circuit.name}.qasm", qasm)

    expected = len(BENCHMARKS) * len(COMPILERS)
    print(f"Logical circuits: {len(BENCHMARKS)}")
    print(f"Expected compiler outputs: {expected}")

    for required, available, name in (("TKET", HAS_TKET, "TKET"), ("Prometheus", HAS_PROMETHEUS, "Prometheus")):
        if not available:
            raise RuntimeError(f"{name} is not importable; full matrix is mandatory.")

    all_records: List[RunRecord] = []
    print("\n[1] LOCAL COMPILATION / ROUTING / CONTRACT AUDIT\n")
    valid_statuses = {"COMPILED_SEMANTIC_PASS", "COMPILED_UNVERIFIED"}
    
    for compiler_name in COMPILERS:
        arm = compile_arm(compiler_name, BENCHMARKS, ideals, backend, root)
        all_records.extend(arm)
        passed = sum(r.status in valid_statuses for r in arm)
        print(f"\n{compiler_name}: {passed}/{len(BENCHMARKS)} passed\n")

    passed = sum(r.status in valid_statuses for r in all_records)
    if passed != expected:
        failures = [asdict(r) for r in all_records if r.status not in valid_statuses]
        write_text(root / "results" / "pre_qpu_failures.json", json.dumps(sanitize_keys(failures), indent=2, default=str))
        raise RuntimeError(f"FULL MATRIX FAILED: {passed}/{expected}. No QPU submission performed.")

    print("\n[2] SINGLE UNIFIED IBM RUNTIME JOB\n")
    job, valid_records, execution_order, sampler_control = submit_unified_job(all_records, backend)
    print(f"Job ID: {job.job_id()}")
    print(f"PUB count: {len(valid_records)}")
    print("Execution order:")
    for item in execution_order:
        print("  " + item)

    terminal = {"DONE", "ERROR", "CANCELLED"}
    while job.status() not in terminal:
        print(f"\rStatus: {job.status()}", end="", flush=True)
        time.sleep(5)
    print()

    if job.status() != "DONE":
        raise RuntimeError(f"Unified IBM Runtime job failed with status {job.status()}")

    print("\n[3] PROCESSING HARDWARE RESULTS\n")
    raw_counts, summary_rows = process_unified_result(job, valid_records, ideals, root)

    job_timestamps = {}
    try: job_timestamps["creation_date"] = str(job.creation_date)
    except Exception: pass
    try: job_timestamps["metrics"] = job.metrics()
    except Exception: pass

    manifest_runs = []
    for record in valid_records:
        clean = asdict(record)
        clean.pop("legalized_circuit_obj", None)
        manifest_runs.append(clean)

    metadata = {
        "benchmark_design": "4 families x 8 sizes x 3 compilers (Micro-Gradient)",
        "logical_circuit_count": len(BENCHMARKS),
        "expected_compiler_runs": expected,
        "backend": BACKEND_NAME,
        "backend_num_qubits": backend.num_qubits,
        "shots_per_pub": SHOTS,
        "bootstraps": BOOTSTRAPS,
        "single_unified_job": True,
        "routing_policy": "compiler_specific",
        "translation_policy": "common_basis_translation_only",
        "common_translation_may_route": False,
        "common_translation_may_introduce_new_2q_edges": False,
        "semantic_verification": "exact_active_physical_statevector_inverse_mapping",
        "semantic_threshold": SEMANTIC_THRESHOLD,
        "job_id": job.job_id(),
        "job_timestamps": job_timestamps,
        "execution_randomization_seed": SEED,
        "execution_order": execution_order,
        "sampler_control_config": sampler_control,
        "hashes": {
            "environment_sha256": environment_hash,
            "target_sha256": target_hash,
        },
        "runs": manifest_runs,
    }
    write_text(root / "manifest.json", json.dumps(sanitize_keys(metadata), indent=2, default=str))

    compiler_summary = []
    for compiler in COMPILERS:
        rows = [r for r in summary_rows if r["compiler"] == compiler]
        compiler_summary.append({
            "compiler": compiler,
            "circuits": len(rows),
            "mean_total_2q_gates": round(float(np.mean([r.get("routed_abstract_2q_gates", 0) for r in rows])), 6),
            "mean_routing_2q_overhead": round(float(np.mean([r.get("routing_induced_2q_overhead", 0) for r in rows])), 6),
            "mean_physical_edges_used": round(float(np.mean([r.get("unique_physical_edges_used", 0) for r in rows])), 6),
            "mean_depth": round(float(np.mean([r.get("depth", 0) for r in rows])), 6),
            "mean_Hellinger_fidelity": round(float(np.mean([r.get("Hellinger_fidelity", 0) for r in rows])), 6),
            "mean_TVD": round(float(np.mean([r.get("TVD", 0) for r in rows])), 6),
            "mean_JSD": round(float(np.mean([r.get("JSD", 0) for r in rows])), 6),
        })
    write_text(root / "results" / "compiler_summary.json", json.dumps(compiler_summary, indent=2))

    print("\n[✓] COMPLETE")
    print(f"Artifacts: {root}")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\n[FATAL]")
        traceback.print_exc()
        raise