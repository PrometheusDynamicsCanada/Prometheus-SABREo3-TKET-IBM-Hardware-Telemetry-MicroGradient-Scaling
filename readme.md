# Prometheus: Hardware-Aware Routing and Physical-Placement/Fidelity Tradeoffs

## For Industry Reviewers

> **If you only read one section, read this.**

Prometheus is an experimental quantum-routing pipeline investigating whether minimizing physical routing cost is always sufficient to maximize execution quality on real quantum hardware.

Across controlled executions on IBM Heron hardware, we observed matched logical circuits where Prometheus used substantially more physical two-qubit operations than conventional routing pipelines, yet produced output distributions with higher measured agreement with the ideal logical computation.

The effect is **not universal**. At larger problem sizes, the additional routing cost eventually ceases to be accompanied by a corresponding fidelity benefit. We therefore do **not** interpret these results as evidence that deeper circuits or additional SWAP operations are intrinsically beneficial.

Instead, the measurements support a narrower hypothesis:

> **Physical routing cost and hardware execution quality can become decoupled when alternative routing changes where the computation is executed on a non-uniform QPU.**

The randomized-instance benchmark provides additional evidence that the effect is not confined to a single hand-selected circuit: across 40 randomized QAOA and CrossEnt instances at N=6–9, Prometheus achieved higher measured fidelity than SABRE O3 in 30 of 40 matched cases (75%).

All hardware payloads, compiled circuits, native translated circuits, measurement counts, and verification scripts are provided for independent audit.

---

# 1. Executive Summary

This repository contains the hardware telemetry and verification artifacts for an experimental study of quantum-computer routing.

The central question is:

> **Can a quantum compiler sometimes obtain a better hardware result by accepting additional routing cost in exchange for a different physical placement of the computation?**

Conventional routing strategies commonly place substantial emphasis on minimizing quantities such as:

- SWAP count
- physical two-qubit gate count
- circuit depth
- routing overhead

This is reasonable because additional two-qubit operations generally introduce additional opportunities for error.

However, a real QPU is not spatially homogeneous. Physical qubits and couplers have different error characteristics, and those characteristics can change over time.

Consequently, two logically equivalent circuits can have very different hardware behavior even when they perform the same logical computation.

Prometheus explores this possibility by deliberately allowing higher routing cost when doing so produces a preferred physical placement.

The experiments reported here identify both:

1. **Benefit regimes**, where higher-cost physical realizations produced substantially higher measured fidelity; and
2. **Scaling boundaries**, where the routing penalty became dominant and the advantage disappeared.

The result is therefore not:

> **"More gates are better."**

It is:

> **"Minimizing routing cost alone may be insufficient to predict hardware execution quality."**

---

# 2. Results at a Glance

| Experiment | Routing cost | Fidelity result | Interpretation |
|---|---:|---:|---|
| **QFT-9** | +242 2Q gates vs SABRE O3 | +0.1197 | Benefit regime |
| **QAOA-6** | +32 2Q gates vs SABRE O3 | +0.1627 | Benefit regime |
| **QAOA-9** | +65 2Q gates vs SABRE O3 | +0.1461 | Benefit regime |
| **QAOA-10** | +89 2Q gates vs SABRE O3 | -0.0591 | Scaling boundary |
| **QFT-10** | +432 2Q gates vs SABRE O3 | -0.0306 | Scaling boundary |
| **Randomized instances** | Higher routing cost | 30/40 wins (75%) | Repeated matched-instance effect |

The fidelity values above are Hellinger fidelities calculated against the exact ideal logical output distribution after inverse physical mapping.

---

# 3. The Scientific Question

A conventional routing objective is straightforward:

> **Minimize the physical cost of routing the logical circuit onto the hardware topology.**

That cost may include:

- SWAP count
- physical 2Q gate count
- circuit depth
- or combinations of these quantities.

The implicit assumption is also straightforward:

> If two implementations perform the same logical computation, the implementation requiring fewer physical operations should generally be preferable.

Prometheus asks whether that assumption is sufficient on a spatially non-uniform QPU.

Specifically:

> **Can a compiler obtain higher hardware fidelity by accepting additional routing operations in exchange for a different physical placement of the logical computation?**

A real processor contains:

- qubits with different error characteristics
- couplers with different error characteristics
- different connectivity regions
- directional gate behavior
- changing calibration conditions
- and other forms of hardware non-uniformity.

Therefore, two logically equivalent physical implementations can experience substantially different hardware environments.

The hypothesis being investigated is that routing should potentially consider both:

**How much does this route cost?**

and:

**Where does this route place the computation?**

---

# 4. Experimental Hardware

### Primary processor

**IBM Heron — `ibm_marrakesh`**

The benchmark was executed using Qiskit Runtime's SamplerV2 execution model.

### Compiler pipelines

The study compares:

- **Qiskit SABRE — Optimization Level 3**
- **TKET**
- **Prometheus**

Prometheus is the experimental routing pipeline under investigation.

The proprietary routing heuristics are not required to reproduce the reported hardware observations. The repository instead provides the resulting compiled circuits and raw hardware payloads needed to audit the measurements.

---

# 5. Experimental Design

The principal benchmark job contains:

- **96 compiled circuit instances**
- multiple compiler pipelines
- multiple circuit families
- multiple problem sizes
- **10,000 shots per circuit**

for a total of:

**960,000 hardware shots**

The circuits were submitted through a unified Qiskit Runtime SamplerV2 execution structure to reduce the possibility that differences between independently scheduled jobs were simply caused by temporal calibration drift.

Dynamical decoupling and Pauli twirling were disabled for this benchmark.

The compiled circuits include:

- QFT
- QAOA MaxCut
- CrossEnt
- GHZ

across multiple problem sizes.

Primary hardware payload:

```text
job-da1do46g52gs73clh7c0
