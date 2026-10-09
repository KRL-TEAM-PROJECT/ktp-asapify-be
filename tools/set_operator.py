"""Run by a project administrator after the user has signed in once; no key files needed."""
import argparse

import firebase_admin
from firebase_admin import auth

p = argparse.ArgumentParser()
p.add_argument("--project", required=True)
p.add_argument("--uid", required=True, help="UID copied from Firebase Authentication after checking the account.")
p.add_argument("--apply", action="store_true")
args = p.parse_args()
app = firebase_admin.initialize_app(options={"projectId": args.project})
user = auth.get_user(args.uid, app=app)
print(f"Account: {user.email}; UID: {user.uid}; email verified: {user.email_verified}")
if not user.email_verified:
    raise SystemExit("Verify the email before granting operator access.")
claims = dict(user.custom_claims or {})
claims["role"] = "operator"
if args.apply:
    auth.set_custom_user_claims(args.uid, claims, app=app)
    print("Operator claim updated. Sign out and sign in again to refresh the token.")
else:
    print("Preview only; --apply grants role=operator and preserves other claims.")
