import firebase_admin
from firebase_admin import auth
import sys
from datetime import datetime

def main():
    try:
        firebase_admin.get_app()
    except ValueError:
        firebase_admin.initialize_app()

    print("Fetching recent users...")
    try:
        page = auth.list_users(max_results=20)
        users = []
        for user in page.users:
            users.append((user.user_metadata.last_sign_in_timestamp or 0, user))
        
        # Sort by last sign in desc
        users.sort(key=lambda x: x[0], reverse=True)
        
        print(f"{'UID':<30} | {'Phone':<15} | {'Provider':<15} | {'Last SignIn'}")
        print("-" * 80)
        for _, u in users:
            provider = u.provider_data[0].provider_id if u.provider_data else "unknown"
            last_in = "Never"
            if u.user_metadata.last_sign_in_timestamp:
                dt = datetime.fromtimestamp(u.user_metadata.last_sign_in_timestamp / 1000)
                last_in = dt.strftime("%Y-%m-%d %H:%M")
            
            print(f"{u.uid:<30} | {u.phone_number or 'N/A':<15} | {provider:<15} | {last_in}")

    except Exception as e:
        print(f"Error listing users: {e}")

if __name__ == "__main__":
    main()
