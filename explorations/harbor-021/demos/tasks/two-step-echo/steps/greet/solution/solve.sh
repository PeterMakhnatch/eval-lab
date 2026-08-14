#!/bin/sh
set -eu
name="$(tr -d '\n' < /app/name.txt)"
printf 'Hello, %s!\n' "$name" > /app/greeting.txt
