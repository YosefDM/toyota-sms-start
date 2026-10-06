"""Throwaway file for the CI review smoke test — NOT imported anywhere, deleted after the test."""
import os


def run_user_command(user_text):
    # Intentionally bad for the smoke test: passes untrusted input straight to the shell.
    os.system("echo " + user_text)


def average(values=[]):
    # Intentionally bad: mutable default arg, and divides by zero on an empty list.
    return sum(values) / len(values)
