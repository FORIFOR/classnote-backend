import firebase_admin
from firebase_admin import auth, credentials
import sys
import os

# usage: python tools/delete_auth.py [uid:XXX | phone:+81... | email:...]
# env: GOOGLE_APPLICATION_CREDENTIALS must be set

def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/delete_auth.py [uid:XXX | phone:+81... | email:...]")
        sys.exit(1)

    # Initialize Firebase Admin
    # Adjust credential path logic if needed, or rely on GOOGLE_APPLICATION_CREDENTIALS
    try:
        firebase_admin.get_app()
    except ValueError:
        firebase_admin.initialize_app()

    target = sys.argv[1]
    kind, value = target.split(":", 1)

    try:
        user = None
        if kind == "uid":
            user = auth.get_user(value)
        elif kind == "phone":
            user = auth.get_user_by_phone_number(value)
        elif kind == "email":
            user = auth.get_user_by_email(value)
        else:
            print(f"Unknown kind: {kind}. Use uid, phone, or email.")
            sys.exit(1)

        print(f"Deleting Auth user: uid={user.uid}, phone={user.phone_number}, email={user.email}")
        auth.delete_user(user.uid)
        print(f"✅ Deleted Auth User: {user.uid}")

    except auth.UserNotFoundError:
        print(f"❌ User not found: {target}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
