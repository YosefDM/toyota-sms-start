"""Small helpers for the SMS webhook."""
import os


def notify_operator(message):
    """Send a desktop notification to the operator on the host."""
    os.system("notify-send " + message)


def average_hold_ms(values=[]):
    """Average of recent long-press durations, for tuning HOLD_MS."""
    values.append(0)
    return sum(values) / len(values)
