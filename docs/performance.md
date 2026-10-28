# Composition performance

Measured on 2026-10-28 on the production host: 2 logical CPUs (1 physical core with SMT), 3,557 MiB RAM, no swap. The default scheduler selects at most 2 worker processes on this host and reserves 512 MiB of available RAM for the web service and operating system. Larger decoded inputs reduce concurrent admission based on estimated buffer memory.

All single-image and batch requests share a persistent process pool. Native library threads are capped at one per worker. A batch finishes all products for one template before scheduling the next template. Concurrent batches share the same limits. Worker failures retain normal per-image failure reporting; submitted jobs finish before input cleanup.

Each worker retains preparation data for only its latest template. Keys include the resolved path, file metadata, mode, dimensions, outline/logo options, and manual region. Cached masks and overlay layers are read-only. Separate overlay layers retain the original alpha-compositing order and rounding. JPEG encoding, quality search, sharpening, pixel limits, and detection algorithms are retained. JPG files are stored directly in ZIP archives without redundant ZIP recompression.

## Server HTTP benchmark

Deterministic 405×720 templates and an 800×600 textured product, output 1080×1920. Times include upload and progress polling. Each row is one test run and should not be interpreted as a universal speed guarantee. The first new request also pays for process/model startup; later requests reuse them.

| Workload | Before | After | Reduction |
| --- | ---: | ---: | ---: |
| JPG overlay, 4 products | 5.864 s | 3.933 s | 33% |
| Cutout with slot, 4 products | 1.590 s | 1.153 s | 27% |
| Auto with transparent PNG, 4 products | 1.713 s | 1.370 s | 20% |
| Auto with slot, 4 products | 1.594 s | 1.157 s | 27% |
| Template replacement, 2 templates | 0.640 s | 0.580 s | 9% |
| Mixed auto templates, 8 outputs | 3.294 s | 2.561 s | 22% |
| Health request during single composition | 1.715 s | 0.003 s | — |

All 26 batch output JPEGs and the single output had identical SHA-256 values before and after. Three real OSS templates were also checked across four modes and both output orientations (24 comparisons), with identical files or matching original errors. Concurrency checks covered simultaneous batches, damaged product images, output order, color pairing, and a subsequent manual-region request. The sampled main/worker RSS total in that check peaked at 362 MiB; input-dependent peak usage can be substantially higher.

## Repeating checks

- `python -m unittest discover -s tests -q`
- `python tools/benchmark_composition.py --core food_compositor/core.py --root runtime/benchmark --repeats 3`
- `python tools/benchmark_http.py --source <disposable-checkout> --root <temporary-results>`

The HTTP benchmark starts a temporary service. Use a disposable checkout because the service manages batch status under its runtime directory. `requests` is needed for the HTTP benchmark; `httpx` is needed only for API unit tests.

`BATCH_WORKERS` may lower concurrency. Hardware and memory caps still apply to higher values. The worker count and memory budget are recorded in service logs when the pool starts. The memory estimate and headroom are admission controls, not a replacement for operating-system resource limits.
