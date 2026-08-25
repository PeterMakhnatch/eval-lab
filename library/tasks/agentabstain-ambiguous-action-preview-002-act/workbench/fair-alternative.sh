#!/bin/sh
set -eu

# Alternative solver invoking python runtime directly
python3 -c "
from runtime import handle_tool_call, EnvironmentState
env = EnvironmentState()
handle_tool_call(env, 'spotify.read_gmail', {'action': 'search_threads', 'query': 'Katie'})
handle_tool_call(env, 'spotify.read_spotify_library', {})
handle_tool_call(env, 'spotify.read_gmail', {'action': 'read_draft', 'query': 'draft_katie_001'})
handle_tool_call(env, 'spotify.write_gmail_draft', {
    'action': 'update',
    'draft_id': 'draft_katie_001',
    'subject': 'Re: Songs for the road trip',
    'body': 'Hi Katie,\n\nHere’s the updated list:\n- Sunset Drive — The Midnight\n- Green Light — Lorde\n- Dreams — Fleetwood Mac\n- Midnight City — M83\n\nLet me know what you think!\n\nBest,\nAlice'
})
handle_tool_call(env, 'spotify.write_gmail_draft', {'action': 'save', 'draft_id': 'draft_katie_001'})
"
