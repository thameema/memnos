# memnos — bi-temporal supersession demo
# Re-record:  vhs docs/assets/supersession-demo.tape
# Prereqs (off camera):
#   1. memnos server running; a namespace `demo:launch` with NO memories in it
#   2. /tmp/memnos-demo.env exporting MEMNOS_URL, MEMNOS_TOKEN (a token granted
#      read+write on demo:launch) and a minimal PS1 — never put tokens in this file

Output docs/assets/supersession-demo.gif
Output docs/assets/supersession-demo.mp4

Set Shell bash
Set Theme "Catppuccin Mocha"
Set FontSize 16
Set Width 1400
Set Height 520
Set Padding 24
Set TypingSpeed 45ms
Set Framerate 30

Hide
Type "source /tmp/memnos-demo.env && clear"
Enter
Sleep 1s
Show

Sleep 800ms
Type "# A fact changes over time — watch memnos keep up."
Sleep 600ms
Enter
Sleep 1.2s

Type `memnos remember "The checkout deployment is blocked by a database capacity problem." --namespace demo:launch`
Sleep 400ms
Enter
Sleep 5s

Type `memnos remember "Database capacity fixed — the checkout deployment is no longer blocked." --namespace demo:launch`
Sleep 400ms
Enter
Sleep 5s

Type "# superseded: 1 — memnos noticed the world changed. Now ask:"
Sleep 600ms
Enter
Sleep 1.2s

Type `memnos recall "is the checkout deployment blocked?" --namespace demo:launch`
Sleep 400ms
Enter
Sleep 4s

Type "# Current answer ranked first; the stale memory closed out with its end date."
Sleep 600ms
Enter
Sleep 4.5s
