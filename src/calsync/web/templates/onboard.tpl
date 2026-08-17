% rebase('layout.tpl', title='Add a team', flash=flash, narrow=True)

<p class="eyebrow">New season</p>
<h1>Paste the feed URL</h1>
<p class="lede">
  From the team app, or from whatever the coach sent. calsync reads the team
  name, the season dates and the venues out of the feed — you confirm three
  things and it goes on a staging calendar.
</p>

<form method="post" action="/onboard" class="card">
  <label class="field url-field">
    <span class="label">Feed URL</span>
    <input type="url" name="url" value="{{ url }}" required autofocus
           spellcheck="false" autocomplete="off"
           placeholder="https://…">
  </label>
  <p class="note" style="margin:-0.5rem 0 1.1rem">
    Nothing is created yet. This fetches the feed once and shows you what is in it.
    If the URL carries a token, the next screen offers to keep it out of the database.
  </p>
  <button class="btn" type="submit">Read the feed</button>
</form>

% if not children:
<h2>No kids on file</h2>
<div class="card">
  <p class="note">A feed has no idea whose team it is. Add whoever this one is for.</p>
  <form method="post" action="/children">
    <input type="hidden" name="next" value="/onboard">
    <div class="row">
      <label class="field">
        <span class="label">Name</span>
        <input type="text" name="name" required autocomplete="off">
      </label>
      <label class="field">
        <span class="label">Initial</span>
        <input type="text" name="initial" maxlength="2" autocomplete="off">
      </label>
    </div>
    <button class="btn" type="submit">Add</button>
  </form>
</div>
% end

<h2>What happens next</h2>
<div class="card">
  <p class="note" style="margin:0">
    A new feed goes to a staging calendar rather than to the family's real ones.
    Subscribe to it on your phone and look at it there — no amount of reading the
    feed text tells you whether a title survives a week view. It stays staged
    until real games appear and parse cleanly, which for a spring team usually
    means weeks. That is the normal path, not a delay.
  </p>
</div>
