# web — the Deterministic Testing visualiser

Live: <https://kalash-somnathe.github.io/deterministic-testing/>

A static page that renders real simulation traces. No backend, no build step, no
CDN, no dependencies: three files plus a folder of JSON.

![The framing and the trace timeline](screenshot.png)

```
web/
  index.html      structure
  app.css         one stylesheet, dark/light aware, no framework
  app.js          one script, hand-rolled SVG charts, no library
  data/           generated — see below
  screenshot.png  the page, for anyone reading this on GitHub
  .nojekyll       empty; GitHub Pages serves the folder as-is
```

## Run it locally

```bash
python scripts/export_web_data.py     # from the repository root
cd web
python -m http.server 8000
```

Then open <http://localhost:8000>.

A server is required: the page reads its data with `fetch`, which browsers refuse
over `file://`. Opening `index.html` directly shows an explanatory message rather
than a blank page.

## Regenerating the data

`scripts/export_web_data.py` is the only thing that writes `web/data/`. It runs the
real framework — `examples.pipeline` through `deterministic_testing.search` — and copies the
committed measurements out of `artifacts/`. Nothing on the page is typed by hand.

```bash
python scripts/export_web_data.py            # skip anything already fresh
python scripts/export_web_data.py --force    # rebuild everything (~90s)
python scripts/export_web_data.py --out /tmp/data
```

It is resumable: each output file is compared against the mtimes of `deterministic_testing/`,
`examples/`, `artifacts/` and the script itself, and a file newer than all of them
is left alone. An interrupted run costs one file, not the set.

It also refuses to produce data that contradicts the repository — the shrunk
scenarios it computes are compared against `artifacts/seed1_minimal.json` and
`artifacts/seed11_claim_minimal.json`, and it exits non-zero if they differ.

| file | contents | size |
| --- | --- | --- |
| `data/manifest.json` | variant matrix, throughput, determinism proof, shrink reports, per-seed outcomes for seeds 0–511 of each variant | 83 KB |
| `data/featured.json` | the four traces the page is built around: seed 1 and seed 11, each as found and as shrunk | 46 KB |
| `data/seeds-buggy.json` | full traces for seeds 0–63, `buggy` consumer | 667 KB |
| `data/seeds-claim.json` | full traces for seeds 0–63, `claim` consumer | 804 KB |
| `data/seeds-fixed.json` | full traces for seeds 0–63, `fixed` consumer | 682 KB |

**2.3 MB total, of which 129 KB loads on first paint.** The three `seeds-*.json`
files are fetched only when you ask for a seed they contain. Adjust `TRACE_SEEDS`
and `SUMMARY_SEEDS` at the top of the export script to trade size against coverage.

## Deploying

The directory is already a complete static site. There is no base-path assumption:
every URL in the page is relative, including the lazy `data/seeds-*.json` fetches,
so it works at a domain root or under a subpath such as
`https://kalash-somnathe.github.io/deterministic-testing/`. For GitHub Pages, put the
contents of `web/` at the root of the publishing branch (for example `gh-pages`);
`.nojekyll` stops Pages running the files through Jekyll. Any other static host works
the same way.

## Reading the trace chart

- **Lanes** are processes: `producer`, `broker`, `worker-a`, `worker-b`, `store`.
- **Arrows** are messages, drawn from the `SEND` event to the `DELIVER` event that
  the network actually produced for it.
- **Gold** means the network interfered. A stub ending in a cross is a dropped
  message; a dotted arrow is a delayed one; two arrows from one send is a duplicate; an
  open circle is a timeout.
- **The crimson rule** is the scheduler step at which an invariant stopped holding.
- **The teal rule** is the cursor. Click any event, or focus the chart and use
  <kbd>←</kbd> <kbd>→</kbd> <kbd>Home</kbd> <kbd>End</kbd> <kbd>PgUp</kbd>
  <kbd>PgDn</kbd>, <kbd>V</kbd> to jump to the violation, <kbd>Space</kbd> to play.

The horizontal axis is simulated time, compressed: segment width is a fractional
power of the elapsed gap, so it stays strictly ordered while a two-second retry
timeout does not squash a burst of microsecond message passing into one pixel. The
largest clock jumps are labelled underneath so the compression is never silent. The
`steps` toggle switches to one column per event if you would rather read it as a
sequence.

## Notes on the shrink view

The two rows are two *different executions*, not a subsequence — removing a fault
renumbers every message after it, which is why shrinking has to re-run each
candidate. The connecting threads are a longest-common-subsequence alignment on
`(kind, process, op, from, to)`, computed in the export script. It is a reading aid
for what survived; the load-bearing claim is only that the shrunk scenario
reproduces the same named invariant failure, which the framework verifies by
running it.
