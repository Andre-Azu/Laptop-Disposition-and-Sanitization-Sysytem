# Strathmore Laptop Recovery & Disposition System

A Django application for university laptop registration, assessment, sanitization, repair, collection, and Parts Bank cashback workflows. SQLite is configured by default; Tailwind CSS is loaded from its CDN in the shared template.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/`. The superuser has IT Administrator access automatically. Other IT staff can request that role during registration; an approved administrator must approve the request from their Profile page.

## Roles and workflows

- Device Managers can register and manage only their own devices, submit self-assessments, request disposition, and view owner-published public inventory.
- Approved IT Administrators can access the operations queues, approve admin requests and donation payouts, assess devices, and verify sanitization certificates.
- Initial purchase value is retained for backend depreciation and reward calculations; public inventory templates do not render it.
- Run `python manage.py test laptops` for the focused workflow tests.

## Configuration

Set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, and `DJANGO_ALLOWED_HOSTS` in the environment before deployment. `DJANGO_DEBUG` defaults to enabled for local development and must be disabled in production.
