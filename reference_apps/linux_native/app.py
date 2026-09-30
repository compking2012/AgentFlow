#!/usr/bin/env python3
"""Actual GTK3 reference client. Requires a real API and a native display."""
import json
import os
import urllib.request

import gi
from ticket_rules import valid_title

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402 -- PyGObject requires selecting GTK before importing it.


class Tickets(Gtk.Window):
    def __init__(self):
        super().__init__(title="AgentFlow Tickets")
        self.set_default_size(700, 500)
        self.endpoint = os.environ.get("AGENTFLOW_API_URL", "http://127.0.0.1:8765")
        self.role = os.environ.get("AGENTFLOW_ROLE", "manager")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(20)
        self.add(box)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Ticket title")
        self.entry.get_accessible().set_name("ticket-title")
        box.pack_start(self.entry, False, False, 0)
        for name, handler in [("Create ticket", self.create), ("Refresh", self.refresh)]:
            button = Gtk.Button(label=name)
            button.connect("clicked", handler)
            box.pack_start(button, False, False, 0)
        self.error = Gtk.Label()
        self.error.get_accessible().set_name("error-message")
        box.pack_start(self.error, False, False, 0)
        self.items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.items.get_accessible().set_name("ticket-list")
        box.pack_start(self.items, True, True, 0)
        self.connect("destroy", Gtk.main_quit)
        self.refresh()

    def request(self, path, payload=None):
        request = urllib.request.Request(self.endpoint + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer reference." + self.role, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.load(response)

    def refresh(self, *_):
        try:
            tickets = self.request("/api/tickets")["tickets"]
            for item in self.items.get_children():
                self.items.remove(item)
            for ticket in tickets:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                title = Gtk.Label(label=ticket["title"])
                title.get_accessible().set_name(ticket["title"])
                row.pack_start(title, True, True, 0)
                assignment = Gtk.Label(label="Assigned: " + (ticket["assignee"] or "none"))
                assignment.get_accessible().set_name("Assignment " + ticket["title"])
                row.pack_start(assignment, False, False, 0)
                for assignee in ("member", "manager"):
                    button = Gtk.Button(label="Assign to " + assignee)
                    button.get_accessible().set_name("Assign " + ticket["title"] + " to " + assignee)
                    button.connect("clicked", self.assign, ticket["id"], assignee)
                    row.pack_start(button, False, False, 0)
                self.items.pack_start(row, False, False, 0)
            self.items.show_all()
            self.error.set_text("")
        except Exception as exc:
            self.error.set_text(str(exc))

    def create(self, *_):
        if not valid_title(self.entry.get_text()):
            self.error.set_text("invalid_title")
            return
        try:
            self.request("/api/tickets", {"title": self.entry.get_text()})
            self.entry.set_text("")
            self.refresh()
        except Exception as exc:
            self.error.set_text(str(exc))

    def assign(self, _button, ticket_id, assignee):
        try:
            self.request(f"/api/tickets/{ticket_id}/assign", {"assignee": assignee})
            self.refresh()
        except Exception as exc:
            self.error.set_text(str(exc))


if __name__ == "__main__":
    window = Tickets()
    window.show_all()
    Gtk.main()
