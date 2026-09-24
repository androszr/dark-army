| No raw hex outside the tokens file | `grep -rnE '#[0-9a-fA-F]{3,8}\b' src/ --include='*.tsx' --include='*.ts' --include='*.css' \| grep -v 'tokens.css' \| grep -v 'themeColor'` | no output |
| No float money math | `grep -rnE '(parseFloat\|Number\()' src/ --include='*.ts' --include='*.tsx' \| grep -vE ':\s*(\*\|//)' \| grep -iE 'price\|quantity\|qty\|fee\|rate\|amount\|cost'` | no output |
| No client-side vendor calls | `grep -rlE 'use client' src/ \| xargs -r grep -lniE '<vendor hosts from docs/context.md>'` | no output |
| Secrets stay server-side | `grep -rl 'process\.env' src/ \| xargs -r grep -L 'server-only'` | no output |
| Both shells addressed (UI plans only) | read the diff | mobile and desktop paths both present |

Notes on those greps: the hex check exempts `themeColor` (browser chrome is
painted before CSS loads, so it cannot be a `var()`) — that is the only
sanctioned literal. The float check skips comment lines so the money module's
own prose does not flag itself forever.
