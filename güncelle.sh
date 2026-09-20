#!/bin/sh
# Listeyi tabletinde üretir ve GitHub'a gönderir.
cd "$(dirname "$0")" || exit 1
git pull --rebase --autostash || exit 1
python olustur.py || exit 1
git add -A
git diff --cached --quiet || git commit -m "Liste guncellendi"
git push
echo
echo "Bitti. rapor.txt icin depoya bakabilirsin."
