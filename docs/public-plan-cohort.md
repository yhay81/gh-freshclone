# Public plan cohort

The public plan cohort makes automatic-detection coverage reproducible instead
of describing only successful field trials. It fixes every target to an exact
public commit and calls the manifest-only planning API. Repository source and
test bodies are not hydrated, and repository code is never executed.

The cohort deliberately contains two groups:

- supported controls that must compile the expected ecosystem plan;
- unsupported controls that must continue to produce no executable plan.

The second group detects unsafe broadening as an `unexpected-plan` regression.
Other outcomes distinguish a lost detector (`missed-plan`), a changed detector
selection (`ecosystem-mismatch`), and an acquisition failure (`error`). This
keeps GitHub availability failures separate from product regressions in the
JSON report.

Run the checked-in cohort:

```shell
uv run python -m benchmarks.public_plan_cohort
```

Retain the machine-readable result:

```shell
uv run python -m benchmarks.public_plan_cohort \
  --output public-plan-cohort-result.json
```

The definition is
[`benchmarks/public-plan-cohort.json`](../benchmarks/public-plan-cohort.json).
Every case has a portable identifier, public `OWNER/REPO` target, full lowercase
commit SHA, optional component, and the complete expected ecosystem set. Inputs
are bounded to 200 cases and are validated before any network request.

The `Public plan cohort` workflow runs weekly and on manual dispatch. It is not
a pull-request gate because public Git availability is outside this project's
control. The workflow still exits non-zero for any mismatch or acquisition
error and retains the complete JSON report as a 30-day artifact.

## Interpreting the metrics

- `matched` is the primary regression metric.
- `detection_rate` is the fraction that produced any executable plan; it is not
  a quality score because the cohort intentionally includes unsupported cases.
- `unexpected-plan` is more serious than ordinary coverage loss: a conservative
  detector selected a command where the fixture expected a safe refusal.
- `median_seconds` and `p95_seconds` are observations, not hard performance
  gates, because remote Git latency varies.

Adding only easy supported repositories would inflate coverage without testing
the safety boundary. Changes to the cohort should therefore explain why a case
is representative and preserve unsupported controls alongside supported cases.
