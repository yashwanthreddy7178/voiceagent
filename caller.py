import os
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

# Configuration
account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
from_number = os.getenv("TWILIO_PHONE_NUMBER")

SERVER_URL = "https://voiceagent-918i.onrender.com" 

client = Client(account_sid, auth_token)

def make_call(to_number, days_due):
    print(f"Calling {to_number}...")
    
    # We can pass parameters via the URL if we want to customize the prompt per user
    # But for now, the prompt is hardcoded in server.py. 
    # To make it dynamic, we'd need to pass ?days_due={days_due} to the webhook 
    # and handle it in the server to update the Deepgram instructions.
    
    call = client.calls.create(
        to=to_number,
        from_=from_number,
        url=f"{SERVER_URL}/incoming-call",
        # record=True # Optional: Record the call
    )
    
    print(f"Call initiated with SID: {call.sid}")

if __name__ == "__main__":
    # Example usage
    # Replace with the number you want to call
    TARGET_NUMBER = input("Enter phone number to call (E.164 format, e.g., +1234567890): ")
    DAYS_DUE = 3 # Default for now
    
    if SERVER_URL == "YOUR_NGROK_URL_HERE":
        print("ERROR: You must update SERVER_URL in caller.py with your ngrok URL.")
    else:
        make_call(TARGET_NUMBER, DAYS_DUE)
