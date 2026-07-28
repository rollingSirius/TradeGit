# TradeGit Sample Journal

This directory is a small local-only sample journal for trying TradeGit without
connecting GitHub.

Run it from the TradeGit project root:

```bash
TRADEGIT_HOME="$(pwd)/examples/sample-journal" python3 -m tradegit analyze --since 180d
TRADEGIT_HOME="$(pwd)/examples/sample-journal" python3 -m tradegit report --since 180d --markdown
TRADEGIT_HOME="$(pwd)/examples/sample-journal" python3 -m tradegit report --since 180d --pdf --output /tmp/tradegit-sample.pdf
```

The records are fictional. They are designed to show the review flow: captured
thesis, closed round trips, one missing stop, a cash event, and behavior tags such
as `fomo`.
