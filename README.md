# PN532 NCMC Reader

This script will help read the offline balance and transaction history. Developed using the Reverse-Engineered NCMC specification from https://nkmason.dev/posts/reversing-ncmc-spec/

### Dependencies:
    pip install pyserial
    PN532 in HSU mode connected to UART

### Usage:
    python3 get_balance.py
    python3 get_balance.py --port /dev/ttyUSB0
    python3 get_balance.py --port /dev/ttyUSB0 --debug

### Flow:
    1. Initialize PN532 over HSU/UART.
    2. Poll ISO14443-A card.
    3. SELECT PPSE -> Read Application Label (Card Type: RuPay Debit/Prepaid).
    4. SELECT AID -> GPO with PDOL -> Extract Tag DF33 Service Balance.
    5. READ Card Information -> PAN (Masked) & Expiry Date.
    6. READ SFI 16 (Log Records) -> Decode Common Service Area & Language.

### Disclaimer:
    This script was generated using LLM/AI tools.
