# Suvoda IRT: Site User Guide

HEALEY ALS Platform Trial - Regimen G (DNL343)

# 1. Study Directory & IRT Configuration

Table columns: Item | Details
- Protocol Number: NCT05842941 (Master & Regimen G Appendix)
- Blinding Architecture: 24-Week Double-Blind (3:1 Active:Placebo) followed by 52–78 Week Open-Label Active Treatment Extension (ATE).
- Study Drug (IMP): DNL343 100 mg stick packs / Matching Placebo
- Suvoda URL: https://healey.suvoda.com

Table columns: Support Category | Contact Information
- Support Category: Suvoda 24/7 Global Helpdesk (Login issues, system errors, emergency unblinding support, missed visits) | Contact Information: US Toll Free: +1-855-555-1234, Email: support@suvoda.com. *Include your Site Number in all emails.
- Support Category: Drug Supply Vendor (CDMO) (Physical shipment tracking, damaged kit escalation) | Contact Information: Fisher Clinical Services, Email: ALS_RegimenG_Supply@fisherclinical.com

# 2. Access & Navigation Basics

System Access: Suvoda IRT utilizes role-based access. Site Study Coordinators (CRCs), Investigators, and Pharmacists will receive a welcome email with a temporary password upon completion of the Delegation of Authority (DoA) log. All critical transactions in Suvoda require 21 CFR Part 11 Electronic Signatures, meaning you must re-enter your username and password to finalize Randomization, Dispensing, or Unblinding actions.

Dashboard Ergonomics: Upon logging in, you will land on the Home Dashboard containing three primary modular cards:

- Subjects: View all registered subjects at your site, their current status, and execute visit transactions (Screen, Randomize, Dispense).
- Shipments: Track incoming IMP shipments, register temperature monitors, and mark shipments as received.
- Inventory: View your site's real-time stock of DNL343/Placebo kits by status (Available, Quarantined, Damaged).

Important: Never dispense an IMP kit to a subject unless it has been explicitly assigned to them by Suvoda. The system manages the double-blind algorithm and ensures the expiration date of the assigned kit extends past the subject's next scheduled visit.

# 3. Managing Patient Journeys

## Step 3.1: Screening a Subject (Days -42 to -1)

To register a subject who has signed the Master and Regimen-Specific Informed Consent Forms:

1. Click the Add Subject button on the Home Dashboard.
2. Enter the Date of Informed Consent and the subject's Year of Birth.
3. Review the summary screen and click Confirm.
4. The system will generate a unique Subject ID. Print or save the automated Screening Confirmation PDF for your source records.

Stratification Data: [Standard Study Default – Biostats Confirmation Required]. During the Randomization step, Suvoda will prompt you to enter the subject's stratification factors (e.g., Time since symptom onset [<18 months vs. >18 months] and Baseline Bulbar function). Ensure this data is verified in the EDC prior to executing IRT randomization.

## Step 3.2: Baseline / Randomization (Day 0)

Once eligibility is confirmed, randomize the subject and dispense the initial drug supply:

1. Navigate to the Subjects list and click on the specific Subject ID.
2. Click the Randomize action button.
3. Enter the required Stratification variables and the Subject Weight (kg).
4. Electronic Signature: Enter your Suvoda username and password to authenticate the randomization transaction. Click Confirm.
5. Suvoda executes the 3:1 (Active:Placebo) blinding algorithm and displays the assigned Kit Numbers.
6. Retrieve the exact assigned cartons from the pharmacy. Print the Dispensation Confirmation PDF and file it in the subject binder.

## Step 3.3: Scheduled Dispensation Visits (Weeks 4, 8, 16, 24)

At subsequent in-clinic visits, the subject will return unused drug and receive new supply.

1. Select the Subject ID from the dashboard and click Dispense.
2. Select the current visit from the dropdown (e.g., Week 4). (Note: Telephone visits at Weeks 2, 12, and 20 do not require IRT dispensation).
3. The system will ask: "Is the subject experiencing a dose-limiting toxicity requiring a dose reduction?"
   - If No, the system assigns the standard 200 mg QD supply (Cartons containing 14 stick packs, instructing 2 packs daily).
   - If Yes, the system logs the dose reduction to 100 mg QD and adjusts resupply algorithms accordingly.
4. Authenticate with your password and dispense the newly assigned Kit Numbers.

## Step 3.4: Transition to Active Treatment Extension (ATE)

At the Week 24 Visit, the subject completes the double-blind period and transitions to the Open-Label ATE period (receiving DNL343 200 mg QD for up to 78 weeks).

1. Select the Subject ID and execute the Week 24 (End of Double-Blind / Start of ATE) visit.
2. The system will automatically recognize the transition to open-label supply.
3. Authenticate and Confirm. Suvoda will assign Open-Label ATE Kits. Do not dispense any leftover double-blind kits to a subject entering the ATE period.
4. Subsequent ATE visits (ATE Wk 4, ATE Wk 8, ATE Wk 16, etc.) will be available sequentially in the subject's dropdown menu.

## Step 3.5: Subject Discontinuation / End of Study

If a subject permanently discontinues study drug or completes the trial:

1. Click the Subject ID and select Discontinue Subject.
2. Enter the Date of Discontinuation and select the primary reason (e.g., Adverse Event, Withdrawal by Subject).
3. Confirm the transaction. This stops all future automated resupply shipments for this subject.

# 4. Investigational Product (IMP) Supply Management

## Step 4.1: Receiving a Shipment

Suvoda automatically triggers predictive resupply shipments to your site based on your screening/randomization activity. When a shipment arrives:

1. Stop the TempTale monitor included in the physical shipment and download the PDF data.
2. Log into Suvoda and navigate to the Shipments tab. Click on the In Transit shipment number.
3. Click Receive Shipment.
4. Answer the prompt: "Were there any temperature excursions?"
   - If No, all kits are moved to Available status.
   - If Yes, the entire shipment is placed in Quarantined status. You must physically quarantine the drug and email the TempTale PDF to the CDMO for disposition.

Damaged Kits: If you open a shipment and find a physically damaged kit, receive the shipment without an excursion, but immediately navigate to the Inventory tab, select the damaged Kit Number, and change its status from Available to Damaged. Suvoda will automatically trigger a replacement shipment if your site's buffer falls below the required threshold.

## Step 4.2: Drug Accountability & Returns

Subjects must return all used and unused stick packs/cartons to the clinic. Site staff must physically count the returns and update the Suvoda system:

1. Navigate to the Subject ID and select Kit Accountability.
2. Locate the previously dispensed Kit Number.
3. Enter the number of Unused Stick Packs returned. The system updates the kit status to Returned, ensuring complete lifecycle tracking for the Sponsor.

# 5. Emergency Unblinding (Code Break)

Emergency unblinding should only be performed if knowledge of the subject's treatment assignment is absolutely necessary for their immediate medical management. Only the Principal Investigator (PI) or authorized Sub-Investigator has the system permissions to execute a code break.

1. Log into Suvoda. From the Home Dashboard, navigate to the Subjects list.
2. Click on the relevant Subject ID.
3. Under the Action menu, select Emergency Unblind.
4. Acknowledge the warning prompt detailing the severity of breaking the blind.
5. Enter the Reason for Unblinding (e.g., Severe Adverse Event requiring emergency intervention).
6. Authentication: You must enter your Suvoda username and password to execute this transaction.
7. Click Reveal Treatment.
8. The system will display whether the subject was on DNL343 200 mg or Placebo. An automated, unblinded alert email is instantly dispatched to the Sponsor Medical Monitor and the Suvoda Helpdesk.

Note: Unblinding a subject automatically discontinues them from further double-blind protocol treatment. Suvoda will prevent any future double-blind kits from being dispensed to this subject.

# 6. Site Frequently Asked Questions (FAQ)

Q: I dispensed a kit in Suvoda, but when I went to the pharmacy, I dropped and damaged it. What do I do?
A: Do not give the damaged kit to the subject. Log into Suvoda, navigate to the Subject ID, and select the Replace Kit action. Select the damaged kit, choose the reason (e.g., Damaged at Site), and enter your password. Suvoda will assign a new, replacement Kit Number to the subject and change the damaged kit's status to Damaged.

Q: A subject missed their Week 4 visit entirely. How do I record this?
A: Navigate to the Subject ID, click on the Action menu, and select Missed Visit. Choose Week 4 from the dropdown. This ensures the system skips the Week 4 dispensation logic and prepares the subject's timeline for the Week 8 visit.

Q: Can I manually order extra drug for my site?
A: No. Suvoda uses a predictive algorithm to manage your site's inventory based on your current screening and active subject data. If you have an unexpected surge in screening and believe your inventory is low, contact the Suvoda Helpdesk or the CRA, who can authorize a manual supplementary shipment.

Q: The TempTale monitor triggered an alarm upon receipt, but we haven't received disposition yet. Can I use the kits?
A: NO. You must mark the shipment as containing an excursion during receipt, which automatically places the kits into Quarantined status in Suvoda. The system will physically block you from dispensing these kits until the Sponsor/CDMO reviews the TempTale data and explicitly changes their status to Available in the system.
