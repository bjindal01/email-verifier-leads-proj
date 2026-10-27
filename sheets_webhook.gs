/**
 * Paste this into the Google Sheet: Extensions > Apps Script.
 * Deploy > New deployment > Web app
 *   Execute as: Me
 *   Who has access: Anyone
 * Put the web app URL in GOOGLE_SHEETS_WEBAPP_URL.
 */
function doPost(e) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  const row = JSON.parse(e.postData.contents);
  sheet.appendRow([
    row.checked_at,
    row.email,
    row.result,
    row.reason,
    row.risky,
    row.disposable,
    row.role,
    row.accept_all,
    row.free,
    row.safe_to_send,
    row.did_you_mean,
    row.flags,
  ]);
  return ContentService
    .createTextOutput(JSON.stringify({ ok: true }))
    .setMimeType(ContentService.MimeType.JSON);
}
