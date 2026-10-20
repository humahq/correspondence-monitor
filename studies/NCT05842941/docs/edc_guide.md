# Medidata Rave EDC User Guide

HEALEY ALS Platform Trial - Regimen G (DNL343)
Data Management & Site Operations Manual | Version 1.0

# 1. Study Overview & Support Directory

Table columns: Item | Details
- Protocol Number: NCT05842941 (Master & Regimen G Appendix)
- Sponsor: Sean M. Healey & AMG Center for ALS / Denali Therapeutics
- EDC Platform: Medidata Rave EDC (Rave Cloud / iMedidata)

Table columns: Support Category | Contact Information
- Support Category: System Access & Login Issues (Passwords, iMedidata, 2FA, Browser issues) | Contact Information: Medidata Global Helpdesk, Email: helpdesk@mdsol.com, Phone: 1-866-515-6044
- Support Category: eCRF Functionality & Queries (Edit checks, dynamic forms, unlocking requests) | Contact Information: Regimen G Lead Data Manager, Email: HealeyDataSupport@datamanagement.com
- Support Category: Clinical Operations & SDV (Protocol interpretation, source verification) | Contact Information: Lead Clinical Research Associate (CRA), *See your site-specific communication plan

# 2. iMedidata Access & Role Selection

Access to the Medidata Rave EDC environment is strictly managed via role-based provisioning governed by your site's Delegation of Authority (DoA) log. All users must maintain an active iMedidata account.

1. Account Setup: You will receive an automated email invitation from iMedidata. Follow the link to activate your account and set up Dual-Factor Authentication (2FA).
2. eLearning Prerequisites: Before the Regimen G study link becomes active on your dashboard, you must complete the assigned Medidata eLearning modules (e.g., Rave EDC Data Entry, Rave EDC PI Signature).
3. Role Selection: Upon clicking the study link, you will be prompted to select your role:
   - Clinical Research Coordinator (CRC): Full data entry, add unscheduled visits, manual query response.
   - Principal Investigator (PI): Data review, query response, and e-Signature authority.
   - CRA / Monitor: Read-only access, Source Data Verification (SDV), and manual query generation.

Navigation Tip: Use the breadcrumb trail at the top left of the screen (e.g., Study Name > Site Name > Subject ID > Folder) to navigate backward. Do not use your browser's "Back" button, as this may result in unsaved data loss.

# 3. Rave Visual Indicators (Icon Key)

Rave utilizes a visual icon system to indicate the status of folders, forms, and individual data fields. Familiarize yourself with the following critical status icons:

- ✓ Complete / Clean: Data entered and no open edit checks.
- ! System Query (Open): Data violates a programmed edit check.
- ? Manual Query (Open): Raised by a CRA or Data Manager.
- ✓ Answered Query: Site has responded; awaiting DM/CRA closure.
- S Requires Signature: Form is clean and ready for PI e-signature.
- L Locked: Form is hard-locked by DM. No edits allowed.

# 4. Navigating the Study Matrix & Visit Schedule

Subject Registration: Subjects are not manually created in Rave. Once a subject is screened and randomized via the study's IRT/IWRS system, the integration automatically pushes the Subject ID into Rave and generates the initial Subject Matrix.

## Schedule of Assessments (SoA) Mapping

The Rave Subject Matrix is organized chronologically to match the Regimen G Protocol:

Table columns: Rave Folder Name | Protocol Equivalent | Key eCRFs Included
- Rave Folder Name: Screening | Protocol Equivalent: Days -42 to -1 | Key eCRFs Included: ICF, Demographics, Med Hx, Inclusion/Exclusion, Baseline Symptoms.
- Rave Folder Name: Baseline | Protocol Equivalent: Day 0 | Key eCRFs Included: Pre-dose Labs, ECG, Baseline PROs (ALSFRS-R, ROADS, ALSAQ-40).
- Rave Folder Name: Week X (Placebo-Controlled) | Protocol Equivalent: Weeks 2, 4, 8, 12, 16, 20, 24 | Key eCRFs Included: Drug Administration, AEs, ConMeds, PK/Biomarkers, Safety Labs. (Note: Wks 2, 12, 20 are phone visits).
- Rave Folder Name: ATE Week X (Open-Label) | Protocol Equivalent: Weeks 26 through 102 | Key eCRFs Included: Dynamically populates only after the Week 24 visit is marked complete.
- Rave Folder Name: Early Termination (ET) | Protocol Equivalent: Out of sequence ET | Key eCRFs Included: Triggered via the "Study Completion / ET" form if subject withdraws early.

## Dynamic & Unscheduled Folders

If a subject requires an out-of-window safety lab, unscheduled PK draw, or repeated screening procedure, you must add an Unscheduled Visit folder:

1. Navigate to the Subject Page matrix.
2. Locate the Add Event dropdown menu at the bottom of the screen.
3. Select Unscheduled Visit and click Add.
4. In the new folder, select the specific forms you need to complete (e.g., Unscheduled Labs) and enter the assessment date. Do not alter scheduled visit dates for out-of-window procedures.

# 5. Form-by-Form Data Entry Guidelines

## Informed Consent & Eligibility

- Re-Consent: If a subject is re-consented (e.g., transition to the ATE period or protocol amendment), record this on the Informed Consent log form by clicking Add a new Log line.
- Inclusion/Exclusion: Master and Regimen-Specific criteria must be evaluated. If "Not Met" is selected for any criterion, the system will fire a hard edit check requiring the selection of a specific failure reason.

## Drug Administration & PK Reconciliation

DNL343 (or placebo) is administered as oral granules (200 mg QD via two 100 mg stick packs). Dosing compliance and PK timing are critical endpoints.

- Dose Modifications: The protocol permits a dose reduction to 100 mg due to intolerability. If the dose is adjusted, you must update the Planned Dose Level field, check "Yes" for Was the dose adjusted?, and select a reason (e.g., Adverse Event).
- In-Clinic Dosing & PK: For Weeks 4, 8, 16, and 24, exact dosing times must be reconciled with PK draw times.
  - Record the Dose Start Time in 24-hour format (e.g., 09:15).
  - Edit Checks: The EDC utilizes hard logic comparing PK times to the Dose Start Time. Pre-dose PK samples must be logged between 0 and 2 hours prior to dosing. Post-dose samples must be logged between 1 and 8 hours post-dose. Violating these windows will fire a system query requiring a protocol deviation explanation.
- PRO Transcription Rule: Patient Reported Outcomes (ALSFRS-R, ALSAQ-40, CNS-BFS, ROADS, PGI-S, PGI-C) must be transcribed into Rave exactly as completed by the patient. Do not interpolate missing responses. If a patient skips a question, mark the field as "ND" (Not Done) to clear the system query.

## Adverse Events (AE) & The SAE Trigger

- Medical History vs. AEs: Pre-existing conditions must be logged on the Baseline Medical History form. If a pre-existing condition worsens in severity or frequency post-dose, log the worsening as a new event on the Adverse Events Log.
- The SAE Trigger Question: On the AE log, the question "Is this adverse event serious?" acts as a system trigger. Checking "Yes" will instantly generate a dynamic Expedited Reporting Evaluation form. You must complete and save this form to alert the Pharmacovigilance (PV) Safety Gateway. [Rave Standard Study Default – Data Management Confirmation Required] Ensure CTCAE v5.0 grading and attribution (Unrelated to Definite) are completed prior to saving.

## Concomitant Medications & Restrictions

Every entry on the Concomitant Medication log requires an Indication. This indication must logically map to an existing term on either the Medical History or Adverse Event form.

Prohibited Medications Check: DNL343 protocol excludes the use of strong CYP3A4/5 inducers and sensitive CYP3A4/5 substrates (e.g., St. John's wort, midazolam, itraconazole). Entering flagged medications into the ConMed log will fire a hard warning query alerting the CRA and Medical Monitor of a potential protocol violation.

# 6. Query Management & Discrepancy Resolution

Queries ensure data integrity. They must be resolved promptly prior to data locks.

- System Edit Checks: Auto-generated queries fired instantly upon saving if data violates a protocol rule (e.g., Out-of-range vital signs, invalid date formats, or missing mandatory fields).
- Manual Queries: Placed by Data Managers, Medical Monitors, or CRAs requesting clarification.

How to Action a Query:

1. To correct erroneous data: Click the edit pencil next to the field. Select "Entry Error" or "New Information" from the dropdown. Input the correct data and click Save. The query will automatically resolve. Do not type the corrected data into the query comment box.
2. To explain data without changing it: If the data is correct but unusual (e.g., a clinically verified extreme heart rate), leave the data field alone. Type your explanation in the Query Response text box beneath the query message and click Save. The query status will change to "Answered" pending DM review.

# 7. Investigator e-Signature (PI & Sub-I)

Under 21 CFR Part 11, Principal Investigators must electronically sign clinical data. Forms will display the purple Requires Signature icon only after all mandatory fields are complete and all queries on that form are closed.

Batch Signing Workflow:

1. The PI logs into iMedidata and navigates to the Subject Matrix.
2. Click the Requires Signature icon at the top of the column header to filter for all signable forms across the subject.
3. Review the data presented.
4. Check the authorization acknowledgment box at the bottom of the page, enter your Medidata login password, and click Sign.

Signature Invalidation Warning: If a CRC or CRA modifies a single data point or responds to a query on a form that has already been signed, the signature is immediately broken. The frozen/padlock icon will lift, and the form will revert to "Requires Signature" status, requiring the PI to re-sign the updated form.

# 8. Site-Specific Frequently Asked Questions (FAQ)

Q: I accidentally created a duplicate log line for an Adverse Event. How do I delete it?
A: Site users do not have security permissions to hard-delete log lines. To remove the entry, check the "Inactivate" box on that specific log line and click Save. The line will strike through. Do not overwrite the line with a different AE.

Q: How do I handle partial or completely unknown start dates for concomitant medications or medical history?
A: Rave utilizes a standard partial date format. If the day is unknown, enter UNK in the day field (e.g., UNK-AUG-2021). If both the month and day are unknown, enter UNK-UNK-YYYY. Leave the date completely blank only if the entire date, including the year, is unknown.

Q: The system is firing an edit check because a lab parameter was not done, but the field only accepts numeric values. How do I clear this?
A: Do not type text (like "ND" or "N/A") into a numeric field. Look for the checkbox next to the field typically labeled "ND" (Not Done) or "UNK" (Unknown). Checking this box overrides the numeric requirement and resolves the system query.

Q: A form is marked with a grey Padlock icon (Locked). Can the site still modify data or answer open queries?
A: No. A Padlock indicates Data Management has hard-locked the form, typically for interim analysis or study closeout. If you discover a critical source data error on a locked form, you must contact the Lead Data Manager to request the form be temporarily unlocked.

Q: The patient withdrew consent during the Active Treatment Extension (ATE). Which forms are mandatory to complete before locking the subject?
A: You must complete the End of Treatment form, the End of Study / Early Termination form (selecting "Withdrawal of Consent" as the primary reason), and ensure all Adverse Events have End Dates or are explicitly marked as "Ongoing at Study Exit."
