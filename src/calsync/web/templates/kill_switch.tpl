% rebase('layout.tpl', title='Revoke every key', flash=flash, narrow=True)

<p class="eyebrow">Settings · emergency</p>
<h1>Revoke every key</h1>

% if error:
<p class="lede"><strong>{{ error }}</strong></p>
% end

<div class="card">
  <p>Every key stops working at once, and each tool needs a new one:</p>
  <ul>
% for k in keys:
    <li>{{ k.label }} · {{ k.scope.replace('_', ' ') }}</li>
% end
    <li>the read API's token (from <span class="raw">.env</span>)</li>
  </ul>
  <p class="note">Calendars, teams and events are not touched. Feed teams keep syncing.</p>
% if password_set:
  <form method="post" action="/settings/kill-switch">
% if feeds:
    <p><label><input type="checkbox" name="pause_feeds" value="1"> Also pause the
      {{ len(feeds) }} feed {{ 'team' if len(feeds) == 1 else 'teams' }}</label></p>
% end
    <input name="password" type="password" placeholder="Admin password"
           autocomplete="current-password" required>
    <button class="btn" type="submit">Revoke every key</button>
  </form>
% else:
  <p><a href="/settings#admin-password">Set an admin password</a> first.</p>
% end
</div>
