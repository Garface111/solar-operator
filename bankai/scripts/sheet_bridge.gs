/**
 * BankAI → Google Sheets bridge.
 *
 * Why this exists: Google's default organisation policy
 * (iam.disableServiceAccountKeyCreation) blocks downloading service-account
 * keys, and relaxing that org-wide so a household tool can write a spreadsheet
 * is a poor trade. A script bound to the sheet needs no key at all — it runs as
 * its owner, who already has access.
 *
 * SETUP (about three minutes)
 *   1. Open the spreadsheet → Extensions → Apps Script.
 *   2. Delete whatever is in Code.gs and paste this file.
 *   3. Change SECRET below to a long random string of your own.
 *   4. Deploy → New deployment → type "Web app".
 *        Execute as:      Me
 *        Who has access:  Anyone
 *      ("Anyone" means anyone with the URL can POST — the SECRET is the gate.
 *       It cannot read your sheet without it.)
 *   5. Authorise when prompted, then copy the /exec URL.
 *   6. Put both values in bankai/.env:
 *        SHEETS_WEBHOOK_URL=<the /exec URL>
 *        SHEETS_WEBHOOK_SECRET=<the same SECRET>
 *
 * The copilot only ever writes to its own tab (default "BankAI Actuals"). Your
 * planning columns and their formulas are never touched.
 */

const SECRET = 'CHANGE-ME-to-a-long-random-string';

function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents);

    if (payload.secret !== SECRET) {
      return ContentService.createTextOutput('unauthorized');
    }

    const tabName = payload.tab || 'BankAI Actuals';
    const values = payload.values || [];
    if (!values.length) {
      return ContentService.createTextOutput('ok: nothing to write');
    }

    const book = SpreadsheetApp.getActiveSpreadsheet();
    let tab = book.getSheetByName(tabName);
    if (!tab) {
      // Insert at the END. insertSheet() otherwise drops the new tab in front,
      // which silently changes which sheet a credential-free CSV export returns
      // — that broke reading the planning ledger the first time this ran.
      tab = book.insertSheet(tabName, book.getSheets().length);
    } else if (book.getSheets()[0].getName() === tabName && book.getSheets().length > 1) {
      // Self-heal a tab that is already sitting in front.
      book.setActiveSheet(tab);
      book.moveActiveSheet(book.getSheets().length);
    }

    // Clear only the block we manage, so a shorter update never leaves
    // yesterday's rows stranded underneath looking current.
    tab.clear();

    const width = Math.max.apply(null, values.map(function (row) { return row.length; }));
    const padded = values.map(function (row) {
      const copy = row.slice();
      while (copy.length < width) { copy.push(''); }
      return copy;
    });

    tab.getRange(1, 1, padded.length, width).setValues(padded);
    return ContentService.createTextOutput(
      'ok: wrote ' + padded.length + ' rows to ' + tabName
    );
  } catch (err) {
    return ContentService.createTextOutput('error: ' + err);
  }
}

/** Visiting the URL in a browser should not look broken — and it names the
 *  tabs, which is the one thing the copilot cannot discover without a key. */
function doGet() {
  const names = SpreadsheetApp.getActiveSpreadsheet().getSheets().map(function (s) {
    return s.getName();
  });
  return ContentService.createTextOutput(
    'BankAI sheet bridge is live. POST to write.\ntabs: ' + names.join(' | ')
  );
}
