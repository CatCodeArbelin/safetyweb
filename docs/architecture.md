# Architecture

Future architecture:

```text
WPF Client -> Windows Service -> Engine Manager -> Xray / Zapret
```

PR-01 is only a bootstrap. It contains no real network logic and does not start Xray, Zapret, WinDivert, proxy, DNS, or route changes.
