
# NCMC Card Balance & Transaction History Reader

A protocol-level implementation for reading card information, offline service balance, and transaction history from compatible NCMC/EMV contactless smart cards.

The project demonstrates how to communicate with a contactless smart card using ISO 14443-A, ISO 14443-4, ISO 7816-4 APDUs, and card-specific data structures.

The same card-processing logic can be adapted to Android, microcontrollers, or other systems capable of exchanging APDUs with a compatible card.

> **Disclaimer:** This project is intended for educational purposes only. Tampering card to get "free" money or "free" transit ticket is a crime. Card behavior, data formats, and application identifiers may vary between card issuers and deployments. Do not assume that undocumented offsets or fixed data structures are universal.

---

## 1. Features

- Detect a compatible contactless smart card.
- Select the payment system environment (PPSE).
- Discover application identifiers (AIDs).
- Select a card application.
- Execute GET PROCESSING OPTIONS (GPO).
- Extract the service balance from tag `DF33`.
- Read card information:
  - PAN
  - Effective date
  - Expiry date
- Read transaction log records.
- Decode Common Service Area (CSA) data.
- Extract transaction details:
  - Acquirer ID
  - Operation ID
  - Terminal ID
  - Transaction timestamp
  - Sequence number
  - Transaction amount
  - Balance before transaction
  - Transaction status
- Reconstruct and display transaction history.

---

## 2. System Architecture

The implementation consists of two independent layers.

```text
+----------------------------------------------------+
|                  Application Layer                 |
|                                                    |
|  Card Selection  |  Balance  |  History  |  Parser  |
+------------------+----------+----------+-----------+
|                  Card Protocol Layer               |
|                                                    |
|       ISO 7816-4 APDU Exchange / Response          |
+----------------------------------------------------+
|               Contactless Transport Layer          |
|                                                    |
|  ISO 14443-A / ISO 14443-4 Card Communication      |
+----------------------------------------------------+
|                    Hardware Layer                  |
|                                                    |
|       Android NFC / MCU / PC Smart Card Reader     |
+----------------------------------------------------+
```

### Transport abstraction

The application should depend on a simple APDU exchange interface:

```python
class CardTransport:
    def transceive(self, apdu: bytes) -> bytes:
        """
        Send an APDU and return the complete card response,
        including SW1 and SW2.
        """
        raise NotImplementedError
```

A platform-specific implementation is responsible for:

1. Initializing the communication hardware.
2. Detecting a card.
3. Establishing the required ISO 14443-4 communication.
4. Sending and receiving APDUs.
5. Handling transport-level errors.

The card-processing layer should not depend on the hardware vendor or transport protocol.

---

## 3. End-to-End Flow

```text
Initialize reader
       |
       v
Detect contactless card
       |
       v
Activate ISO 14443-4 communication
       |
       v
SELECT PPSE
       |
       v
Parse PPSE response and discover AID
       |
       v
SELECT application AID
       |
       v
Build PDOL data
       |
       v
GET PROCESSING OPTIONS (GPO)
       |
       +----> Extract DF33 service balance
       |
       v
Read card information
       |
       +----> PAN
       +----> Effective date
       +----> Expiry date
       |
       v
Read transaction log (SFI 16)
       |
       v
Extract Common Service Area
       |
       +----> Validation data
       +----> History data
       +----> Language
       |
       v
Merge and reconstruct history
       |
       v
Convert timestamps and amounts
       |
       v
Print card details and transaction history
```

---

## 4. Card Communication Initialization

The first stage is establishing a valid contactless card communication session.

### 4.1 Reader initialization

The platform-specific transport implementation should:

- Initialize the NFC/contactless interface.
- Configure the supported communication parameters.
- Wait for a card to be presented.
- Activate the card using the appropriate ISO 14443 procedures.
- Establish ISO 14443-4 communication if supported.

The application should verify that the detected card is compatible before sending EMV APDUs.

### 4.2 Card capability verification

The card activation response provides information such as:

- SENS_RES (ATQA)
- SAK
- UID
- ATS, where available

The implementation checks the SAK to verify ISO 14443-4 capability.

The SAK check is a preliminary compatibility check. It does not guarantee that the card supports the specific EMV application or NCMC data structures.

### 4.3 APDU transport contract

The transport layer must provide the application with the complete card response:

```text
Response APDU = Response Data || SW1 || SW2
```

For example:

```text
6F ... 90 00
```

The application must separate the response body from the final two status bytes.

---

## 5. APDU Response Handling

### 5.1 Status word

The final two bytes of a response APDU are SW1 and SW2.

```python
def parse_sw(response: bytes) -> int:
    if len(response) < 2:
        raise ValueError("Response too short")

    return int.from_bytes(response[-2:], "big")
```

A successful command generally returns:

```text
90 00
```

The implementation must check the status word before parsing response data.

### 5.2 Common status words

| Status | Meaning |
|---|---|
| `9000` | Command completed successfully |
| `6Cxx` | Wrong length; `xx` indicates the expected length in applicable cases |
| Other values | Command-specific error or unsupported operation |

When the card returns `6Cxx`, retrying with the indicated length may be appropriate for commands that support this behavior.

Do not blindly retry all error status words.

---

## 6. BER-TLV Parsing

EMV responses contain BER-TLV encoded data.

```text
Tag || Length || Value
```

Example:

```text
50 0B 52 55 50 41 59 ...
```

Where:

- `50` = Application Label
- `0B` = Length
- Following bytes = Label value

### 6.1 Required parser functionality

The TLV parser should support:

- Single-byte tags.
- Multi-byte tags.
- Short-form lengths.
- BER long-form lengths.
- Nested constructed TLVs.
- Recursive tag searching.

The implementation uses recursive searching to locate tags within constructed templates such as `6F`, `A5`, and other response structures.

### 6.2 Important considerations

- Validate that tag and length fields do not exceed the response buffer.
- Reject malformed or truncated TLVs.
- Do not assume that the desired tag occurs at a fixed offset.
- Preserve the raw value bytes for debugging and future analysis.

---

## 7. Application Discovery — SELECT PPSE

The first EMV application command is SELECT PPSE.

### Command

```text
00 A4 04 00 0E
32 50 41 59 2E 53 59 53 2E 44 44 46 30 31
00
```

The application identifier is:

```text
2PAY.SYS.DDF01
```

### Purpose

The PPSE response is used to discover applications available on the card.

The implementation extracts:

- Application Label (`50`)
- Application Identifier (`4F`)

### Processing

1. Send SELECT PPSE.
2. Check the status word.
3. Parse the response as BER-TLV.
4. Search recursively for tag `4F`.
5. Store all discovered AIDs.
6. Select a compatible application.

### Important

The current implementation selects the first AID found. A portable implementation should inspect the discovered application entries and select an application according to the application's intended use and supported data.

---

## 8. Application Selection — SELECT AID

After discovering the AID, select the application.

### Command structure

```text
00 A4 04 00 Lc AID Le
```

Where:

- `Lc` = length of the AID.
- `AID` = application identifier discovered in the PPSE response.
- `Le` = expected response length.

The AID must be obtained from the card's PPSE response rather than assumed to be identical for all cards.

### Processing

1. Construct SELECT AID.
2. Send the command.
3. Verify `SW = 9000`.
4. Parse the application response.
5. Identify the processing requirements, including PDOL if supplied.

---

## 9. GET PROCESSING OPTIONS (GPO)

GPO initializes application processing and returns application-related data.

The command requires PDOL data when the application specifies a Processing Data Object List.

### 9.1 PDOL

A PDOL describes the data elements expected by the card.

Each PDOL entry consists of:

```text
Tag || Length
```

The terminal must construct the corresponding value field in the same order and with the required lengths.

The current implementation uses a captured PDOL byte sequence:

```text
FF80F00001
0040000000
000848
0002
0743
260206
060235
CFBAFEBD
FF01
3032343638373000
```

The values are application-specific and should not be assumed to be universal terminal values.

### 9.2 GPO command format

When using a PDOL, the command data is wrapped in an `83` template:

```text
83 || Length || PDOL Values
```

The command structure is:

```text
80 A8 00 00 Lc 83 Length PDOL_Data Le
```

### 9.3 Processing

1. Read or determine the required PDOL definition.
2. Construct the PDOL values in the correct order.
3. Wrap the values using tag `83`.
4. Send GPO.
5. Check the response status word.
6. Parse the returned response template.
7. Extract the service balance if present.

---

## 10. Service Balance — Tag DF33

The implementation searches for tag `DF33` in the GPO response.

The current decoder expects the balance to be contained in a card-specific 29-byte structure:

```python
balance_bytes = df33_data[23:29]
```

The extracted six-byte value is interpreted as a decimal BCD amount in paise:

```python
balance_paise = int(balance_bytes.hex())
balance_rupees = balance_paise / 100.0
```

### Example

If the extracted BCD value represents:

```text
000000001250
```

Then the balance is interpreted as:

```text
₹12.50
```

### Porting requirements

The `DF33` data layout is an implementation-specific assumption. Verify the data structure against actual card captures before using it with another card variant.

The balance must not be inferred solely from the length of the tag. Validate:

- Expected data length.
- BCD validity.
- Currency scaling.
- Sign or special encoding rules, if applicable.
- Whether the field represents the current balance or another service value.

---

## 11. Reading Card Information

The implementation reads records from SFI 1 and SFI 2.

### READ RECORD command

```text
00 B2 P1 P2 00
```

Where:

```text
P1 = Record number
P2 = (SFI << 3) | 0x04
```

For SFI 1:

```text
P2 = 0x0C
```

For SFI 2:

```text
P2 = 0x14
```

### Processing sequence

```text
For SFI in [1, 2]:
    For Record in [1, 2, 3]:
        READ RECORD
        Check status
        Search response for card information tags
```

The implementation continues when a record read fails and searches the remaining records.

### 11.1 PAN — Tag 5A

Tag `5A` contains the Application PAN in BCD format.

The implementation:

1. Extracts tag `5A`.
2. Converts the bytes to hexadecimal.
3. Removes trailing `F` padding.
4. Stores the PAN.
5. Masks the PAN before printing.

Example masking:

```text
1234567890123456
123456******3456
```

The complete PAN should not be unnecessarily exposed in logs or user interfaces.

### 11.2 Track 2 Equivalent Data — Tag 57

If tag `5A` is not available, the implementation checks tag `57`.

Track 2 equivalent data commonly contains:

```text
PAN || D || Expiry Date || ...
```

The implementation extracts the PAN and expiry date from the hexadecimal representation when the separator `D` is present.

The exact structure must be validated before relying on it for card processing.

### 11.3 Effective Date — Tag 5F25

Tag `5F25` is interpreted as a six-digit BCD date:

```text
YYMMDD
```

The decoder converts it to:

```text
YYYY-MM-DD
```

The effective date is also retained as a datetime object for timestamp reconstruction.

### 11.4 Expiry Date — Tag 5F24

Tag `5F24` is interpreted as:

```text
YYMMDD
```

The implementation displays the date as:

```text
YYYY-MM-DD
```

---

## 12. Transaction Log — SFI 16

The transaction log is read from:

```text
SFI = 16
```

The current implementation reads a configurable number of records, with a default of 10.

```python
for record in range(1, count + 1):
    data = read_record(16, record)
```

Each record is checked for sufficient data length before decoding.

The implementation uses the final 96 bytes of the record:

```python
csa_data = data[-96:]
```

This is a card-specific extraction rule. The record format should be verified against the card's actual response before porting.

---

## 13. Common Service Area (CSA)

The decoder expects a 96-byte Common Service Area.

```text
CSA = 96 bytes
```

The data is divided into:

| Section | Size |
|---|---:|
| General Data | 2 bytes |
| Validation Data | 19 bytes |
| History Data | 68 bytes |
| Remaining data | 7 bytes |

The history area contains four entries of 17 bytes each:

```text
68 bytes / 4 = 17 bytes
```

### CSA layout

```text
+--------------------+-------+
| General Data       | 2 B   |
+--------------------+-------+
| Validation Data    | 19 B  |
+--------------------+-------+
| History Entry 1    | 17 B  |
+--------------------+-------+
| History Entry 2    | 17 B  |
+--------------------+-------+
| History Entry 3    | 17 B  |
+--------------------+-------+
| History Entry 4    | 17 B  |
+--------------------+-------+
| Remaining Data     | 7 B   |
+--------------------+-------+
| Total              | 96 B  |
+--------------------+-------+
```

---

## 14. Language Decoding

The language is extracted from the second byte of the CSA:

```python
lang_byte = csa_bytes[1]
lang_code = (lang_byte >> 3) & 0x1F
```

The implementation maps the resulting five-bit code to a language name.

The supported lookup table includes Indian languages such as:

- English
- Hindi
- Bengali
- Marathi
- Telugu
- Tamil
- Gujarati
- Urdu
- Kannada
- Malayalam
- Punjabi
- Sanskrit
- Assamese
- Nepali
- Sindhi
- Dogri
- Konkani
- Manipuri
- Bodo

Unknown values are represented as reserved/unknown codes.

---

## 15. Validation Data

Validation data begins at:

```text
CSA offset = 2
Length = 19 bytes
```

The implementation extracts:

| Field | Offset within validation data | Length |
|---|---:|---:|
| Acquirer ID | 2 | 1 byte |
| Operation ID | 3 | 2 bytes |
| Terminal ID | 5 | 3 bytes |
| Time in minutes | 8 | 3 bytes |
| Maximum fare | 11 | 2 bytes |
| Status byte | 18 | 1 byte |

All multi-byte numeric fields are decoded in big-endian order.

### Validation status

The upper nibble of the status byte is used as the transaction status code:

```python
status_code = (status_byte >> 4) & 0x0F
```

The current mapping is:

| Code | Meaning |
|---:|---|
| `0` | Exit |
| `1` | Entry |
| `2` | Penalty Apply |
| `3` | One Tap / Ticket |

These mappings are implementation-specific and should be validated for the target card system.

---

## 16. History Data

History data starts at:

```text
CSA offset = 21
Length = 68 bytes
```

It contains four 17-byte entries.

```python
for index in range(4):
    entry = history_bytes[index * 17:(index + 1) * 17]
```

### 16.1 History entry format

| Field | Offset | Length |
|---|---:|---:|
| Acquirer ID | 0 | 1 byte |
| Operation ID | 1 | 2 bytes |
| Terminal ID | 3 | 3 bytes |
| Time in minutes | 6 | 3 bytes |
| Sequence number | 9 | 2 bytes |
| Amount | 11 | 2 bytes |
| Balance/status | 13 | 3 bytes |

All multi-byte numeric values are interpreted as big-endian.

### 16.2 Transaction amount

The implementation interprets the two-byte amount as a value in units of ₹0.10:

```python
amount = raw_amount / 10.0
```

This scaling is a card-specific assumption.

### 16.3 Balance before transaction

The balance/status field is decoded as a 24-bit integer.

```python
raw = int.from_bytes(data[13:16], "big")
balance_raw = (raw >> 4) & 0xFFFFF
balance_before = balance_raw / 10.0
```

The lower four bits are used as the transaction status code.

The balance scaling and bit allocation should be verified against the intended card specification or captured transactions.

---

## 17. Merging Validation and History Events

The card stores information in two related structures:

- Validation data
- History data

The implementation merges the two using:

```text
(minutes, acquirer ID, operation ID, terminal ID)
```

as the matching key.

### Processing

1. Create a map of validation events.
2. Create a map of history events.
3. Find the union of all event keys.
4. Match validation and history records.
5. Merge fields from both sources.
6. Retain records that occur in only one source.
7. Calculate balance after transaction where possible.

### Balance calculation

For a matched history event:

```text
Balance after = Balance before − Amount
```

The implementation treats the amount as a debit for its reconstructed table.

For unmatched records, the available information is retained, and missing fields are represented with placeholders.

---

## 18. Top-Up Detection

The implementation sorts reconstructed events chronologically and compares consecutive balances.

If the current transaction's balance before is greater than the previous balance after, it infers a top-up:

```text
Top-up amount =
    Current balance before − Previous balance after
```

The generated record is labeled:

```text
Top-Up
```

### Limitations

This is an inference based on balance differences, not a cryptographically authenticated top-up transaction record.

Balance increases may have other causes, including:

- Missing history records.
- Incomplete record reads.
- Incorrect scaling or decoding.
- Unrecognized transaction types.
- Data inconsistencies.

An embedded or production implementation should distinguish between:

1. Explicitly encoded top-ups.
2. Inferred balance changes.
3. Unknown or incomplete records.

---

## 19. Timestamp Reconstruction

The card stores transaction time as a number of minutes.

The implementation reconstructs a timestamp using the effective date:

```python
datetime = effective_date + timedelta(minutes=minutes)
```

The resulting value is formatted as:

```text
YYYY-MM-DD HH:MM
```

### Important considerations

- Confirm the card's time origin and unit.
- Determine whether the minute counter resets at a particular boundary.
- Check timezone assumptions.
- Validate date rollover.
- Do not assume the effective date is always the correct base date without card-specific verification.

If the effective date is unavailable, the implementation displays the raw minute count.

---

## 20. Final Output

The implementation prints:

### Card information

- Masked PAN
- Expiry date
- Card type
- Card language
- Current balance

### Transaction history

| Field | Description |
|---|---|
| Seq # | Transaction sequence number |
| Date & Time | Reconstructed timestamp |
| Acq ID | Acquirer identifier |
| Op ID | Operation identifier |
| Term ID | Terminal identifier |
| Max Fare | Maximum fare from validation data |
| Amount | Debit or inferred top-up |
| Balance | Calculated balance after transaction |
| Status | Transaction status |

The history is displayed in descending chronological order, with the newest records first.

---

## 21. Platform Porting Guide

### Android

The Android implementation should separate NFC tag activation from APDU processing.

Suggested components:

```text
NfcReader
    |
    +-- CardActivation
    +-- IsoDepTransport
    +-- ApduExchange
    +-- EmvApplication
    +-- TlvParser
    +-- CsaDecoder
    +-- HistoryProcessor
```

The application layer should send APDUs through the platform's ISO-DEP interface and receive the card's response bytes.

The APDU construction, TLV parsing, CSA decoding, and history reconstruction can be implemented independently of Android UI components.

### Microcontroller

A microcontroller implementation should use a hardware abstraction layer:

```c
typedef struct {
    int (*transceive)(
        const uint8_t *tx,
        size_t tx_len,
        uint8_t *rx,
        size_t rx_capacity,
        size_t *rx_len
    );
} card_transport_t;
```

The transport layer handles:

- Card activation.
- ISO 14443-4 communication.
- APDU transmission.
- Response reception.
- Timeout handling.

The application layer handles:

- PPSE selection.
- AID selection.
- GPO.
- READ RECORD.
- TLV decoding.
- CSA decoding.
- History processing.

### Embedded memory considerations

For a resource-constrained MCU:

- Use a streaming TLV parser where practical.
- Avoid unnecessary copies of response buffers.
- Use fixed-size structures for decoded records.
- Validate all lengths before accessing fields.
- Use integer arithmetic for monetary values instead of floating point.
- Limit the number of transaction records retained in RAM.
- Define explicit endianness conversion helpers.
- Store raw records only when required for debugging.

---

## 22. Error Handling

The implementation should distinguish between:

### Transport errors

- No card detected.
- Card removed during communication.
- Communication timeout.
- CRC or framing errors.
- Unsupported transport operation.

### APDU errors

- Invalid status word.
- Application not found.
- Record not available.
- Incorrect command data.
- Unsupported instruction.

### Parsing errors

- Invalid TLV length.
- Missing expected tag.
- Invalid BCD.
- Incomplete CSA.
- Unexpected record size.
- Invalid numeric field.

A malformed record should not cause an unsafe memory access or an incorrect balance to be silently accepted.

---

## 23. Security and Privacy

This project reads sensitive card information and transaction data.

Implementations should:

- Mask PANs in logs and user interfaces.
- Avoid exposing complete card identifiers unnecessarily.
- Do not store raw APDU responses unless required for testing.
- Protect captured card data.
- Avoid treating decoded fields as authenticated without verifying the applicable security mechanisms.
- Do not assume that reading card data authorizes transactions or validates settlement information.

Reading a balance or history record is different from validating the authenticity and integrity of the card's transaction state.

---

## 24. Known Limitations

The current implementation uses several assumptions that should be reviewed before general-purpose deployment:

1. A fixed captured PDOL is used for GPO.
2. The first AID returned by PPSE is selected.
3. The `DF33` balance layout is interpreted using fixed offsets.
4. The CSA layout and field scaling are card-specific.
5. The final 96 bytes of each transaction record are treated as CSA data.
6. A limited number of records are read by default.
7. Top-ups are inferred from balance differences.
8. Date reconstruction depends on the effective date and the assumed minute counter.
9. The transaction status mapping is based on the current decoder's assumptions.

These limitations should be explicitly validated when supporting other card issuers, card products, or deployments.

---

## 25. Development Workflow

When porting this project to a new platform:

1. Implement and test the contactless transport.
2. Confirm that raw APDU exchanges are working.
3. Implement SELECT PPSE and parse its response.
4. Implement AID selection.
5. Implement dynamic PDOL processing.
6. Test GPO and verify the returned application data.
7. Implement a generic READ RECORD function.
8. Extract and validate card information.
9. Read transaction records.
10. Decode CSA using validated format definitions.
11. Implement history reconstruction.
12. Compare decoded values against known card captures.
13. Add unit tests for TLV parsing, BCD decoding, amount scaling, and history merging.

---

## 26. Conclusion

The core of the project is the separation between **card communication** and **application-specific data processing**.

A platform-independent implementation should be built around a generic APDU transport interface. This allows the same EMV application selection, balance extraction, card information decoding, and history processing logic to be reused across Android, microcontrollers, and desktop systems.

The card-specific portions—PDOL values, service balance structure, CSA layout, and monetary scaling—must be validated independently before the implementation is generalized.
