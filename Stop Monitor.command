#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
/opt/homebrew/opt/python@3.13/bin/python3 -m studentwerk_monitor stop
printf '\nPress Return to close this window.\n'
read -r

