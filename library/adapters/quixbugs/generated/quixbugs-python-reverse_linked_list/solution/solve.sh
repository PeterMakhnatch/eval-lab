#!/usr/bin/env bash
set -euo pipefail
cat > /app/python_programs/reverse_linked_list.py <<'QUIXBUGS_REFERENCE_SOLUTION_EOF'

def reverse_linked_list(node):
    prevnode = None
    while node:
        nextnode = node.successor
        node.successor = prevnode
        prevnode = node
        node = nextnode
    return prevnode

"""
def reverse_linked_list(node):
    prevnode = None
    while node:
        nextnode = node.successor
        node.successor = prevnode
        prevnode, node = node, nextnode
    return prevnode

def reverse_linked_list(node):
    prevnode = None
    while node:
        nextnode = node.successor
        node.successor = prevnode
        node, prevnode = nextnode, node
    return prevnode

"""
QUIXBUGS_REFERENCE_SOLUTION_EOF
