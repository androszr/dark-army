# Dark Army IDE Bridge

Lets the Dark Army daemon focus the integrated terminal tab of a Claude Code session in this window.

## The Matrix look

When Dark Army's **Cyber cursor** switch is on (Settings, General, Hosted
terminal; on by default), a window open on a folder enrolled in Dark Army and
trusted in VS Code gets a steady green block cursor and green terminal colours.
They are written into that project's own settings (`.vscode/settings.json`)
as a small closed set: six `workbench.colorCustomizations` entries
(`terminalCursor.*`, `terminal.foreground`, `terminal.ansiGreen`,
`terminal.ansiBrightGreen`, `terminal.selectionBackground`) and
`terminal.integrated.cursorStyle` / `cursorBlinking`. Nothing you set yourself
is overwritten. Switching the Cyber cursor off, or un-enrolling the folder,
removes exactly the entries Dark Army wrote and nothing else. Untrusted
folders, folders that are not enrolled and windows that mix enrolled and
un-enrolled folders are left alone. The colours are the still half of the
hosted terminal's look, ported from the MIT-licensed ghostty-cursor-fx project.
