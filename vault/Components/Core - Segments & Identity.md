# Core - Segments & Identity (`scionarena.core.segments`)

> [segments.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/segments.py)

---

## 📌 Role & Responsibilities
Simulates the SCION path beaconing control plane, beacon expiration, re-signing, and end-to-end path composition.

## 🧱 Key Types
* [`Segment`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/segments.py#L112): Single-direction path segment (Up `SEG_UP`, Core `SEG_CORE`, Down `SEG_DOWN`).
* [`Path`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/segments.py#L224): Composed path joining $Up \oplus Core \oplus Down$ segments.
* [`SegmentStore`](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/core/segments.py#L380): Segment repository and beacon lifecycle scheduler.

## 🔑 Path Identifiers & Hashing
```python
# 1. Structural ID (survives re-signing):
structural_hash(ifaces) -> 64-bit Blake2b hash of interface sequence

# 2. Segment ID (changes on refresh):
_material_hash(structural_id, generation, signature_id) -> 64-bit Blake2b hash
```

## 🔍 Path Search & Bounded Composition
* **Up-Segments**: Constructed once per AS up to core roots.
* **Core-Segments**: Discovered via bounded **beam search** between core ASes to prevent exponential explosion. Empty beams fall back to visited-set shortest path.
* **Lazy Materialization**: End-to-end paths for $(src, dst)$ scopes are composed on-demand and cached in an LRU store (`maxsize=1024`).
