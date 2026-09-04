# World Cup paper — source

`worldcup-paper.tex` is the source for `docs/polymarket-worldcup-paper.pdf`.
Title: *Synthetic Fair-Value Strategy*. MLA layout (Times 12pt, double-spaced,
`Ginzburg N` running header), structured to match C. Byhre, *Unhalt Reversion
Strategy*: numbered sections in the order
Introduction → Data Pipeline → Signal Identification → Data Analysis →
Statistical Testing → Risk → Execution Model → Final Weights → Results →
Conclusion.

## Rebuild

```bash
python docs/paper/make_figures.py          # regenerate figures/*.png
pdflatex worldcup-paper.tex                # run twice; needs Times + newtx (any TeX Live / MiKTeX)
```

The figures are generated from the numbers in the paper (spread/depth by leg,
the logical-floor violation decay, the cross-market gap curve, the Monte Carlo
benchmark, the calibration buckets, the SPY comparison, the capacity chart). The
SPY comparison and equity curve are drawn from the trade record's documented
anchor points, not a fabricated daily series.
