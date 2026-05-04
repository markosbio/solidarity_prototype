from models import Provider

def pay_provider(provider_id, amount):
    provider = Provider.query.get(provider_id)
    if not provider:
        print(f"ERROR: Provider {provider_id} not found")
        return False
    # For prototype: just log the payment
    print(f"💰 PAYMENT: ${amount} to {provider.name} ({provider.payment_details})")
    # In production, integrate M-Pesa STK Push here
    return True
