# Saving a page from anywhere

Find something to read, send its link, and it is on the reader the next time
the reader syncs. Nothing has to be near the reader, and File Transfer is never
opened.

There are three ways to send. All of them end in the same place: an item in the
library, queued for one reader.

| From | How | Needs |
|------|-----|-------|
| An agent | The MCP tool `save_url` ([`mcp.md`](mcp.md#save_url)) | The MCP server |
| Any script | `POST /api/library/url` ([`api.md`](api.md#post-apilibraryurl)) | An API key |
| An iPhone or iPad | A Share Sheet shortcut (below) | An API key, and a route to the main app |

## What the server does with a link

1. It fetches the page. Only public `http(s)` addresses are fetched; the
   limits are listed in [`api.md`](api.md#post-apilibraryurl).
2. It keeps the article and drops the rest: navigation, forms, scripts, images,
   comment sections and link lists. The code is `xteink/server/article.py`.
3. pandoc converts the result to EPUB, with the page's title and author.
4. With `device`, the item is queued for that reader.

Two kinds of page cannot be saved by link:

- A site that refuses automated requests answers `403` (`fetch_failed`). The
  request says honestly that it is `xteink-capture`; it does not pretend to be
  a browser.
- A page behind a login, or one that only has text after JavaScript runs
  (`no_article`).

For those, send the page itself from the browser that can read it. The second
shortcut below does that.

## The phone must reach the main app

Both shortcuts call the main app (port 8780), not the device app. At home that
is the server's LAN address. Away from home it is whatever private route you
use to the server, for example a tailnet. The device tunnel does not help here:
it exposes only `/api/device/*`. See [`remote-access.md`](remote-access.md).

Mint a key for the phone alone, so it can be revoked without touching anything
else:

```bash
python -m xteink.server create-key iphone
```

## Shortcut 1: send a link

This works from any app that shares a link. These steps were written from the
Shortcuts action list and have not been run on a phone from this repository.

1. In Shortcuts, make a new shortcut named **Send to reader**. In its details,
   turn on **Show in Share Sheet** and accept **URLs** and **Safari web pages**.
2. Add **Get URLs from Input**, taking the **Shortcut Input**.
3. Add **Get Contents of URL**:
   - URL: `http://<server>:8780/api/library/url`
   - Method: `POST`
   - Headers: `Authorization` = `Bearer <api-key>`
   - Request Body: `JSON`, with two text fields: `url` = the **URLs** from
     step 2, and `device` = the reader's name.
4. Add **Get Dictionary Value**: get the value for `item.title` in the result.
5. Add **If** that value **has any value**: **Show Notification**
   `Sent: <the title>`. **Otherwise**: get the value for `detail` in the result
   of step 3 and show it. `detail` is the server's reason, for example
   `the site answered HTTP 403`.

## Shortcut 2: send the page itself

This works in Safari only. Safari has already loaded the page, so a login or a
site that refuses automated requests is not in the way. The server does not
fetch anything: the article is uploaded as a file.

1. Make a shortcut named **Send page to reader**, shown in the Share Sheet for
   **Safari web pages** only.
2. Add **Get Article using Safari Reader**, taking the **Shortcut Input**.
3. Add **Get Details of Article**: get the **Body**.
4. Add **Make HTML from Rich Text**, taking that body.
5. Add **Set Name**: name the HTML `article.html`.
6. Add **Get Contents of URL**:
   - URL: `http://<server>:8780/api/library`
   - Method: `POST`
   - Headers: `Authorization` = `Bearer <api-key>`
   - Request Body: `Form`, with `file` (File) = the renamed HTML, and three
     text fields: `title` = the article's **Title**, `kind` = `article`,
     `device` = the reader's name.
7. Show the result as in steps 4 and 5 of the first shortcut.

## When it reaches the reader

The item waits in the reader's queue until the reader syncs. On the X4 build of
the firmware that is on a power-button wake (at most one network attempt per 15
minutes), on the daily run, and on **Sync books now**. Articles land in
`/Articles` and books in `/Books`.
