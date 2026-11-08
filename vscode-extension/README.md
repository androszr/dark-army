# Dark Army IDE Bridge

Lets the Dark Army daemon focus the integrated terminal tab of a Claude Code session in this window.

## No terminal colours

Version 0.1.23 gave a window open on an enrolled, trusted folder a green block
cursor and green terminal colours, written into that project's workspace
settings. From 0.1.24 the extension writes no colours or cursor settings. On
its first start in such a window it takes back exactly the entries 0.1.23
wrote, and only where each still holds the value written; anything you set
yourself, or changed since, stays. Dark Army's own hosted terminal keeps its
cyber cursor (Settings, General, Hosted terminal).
