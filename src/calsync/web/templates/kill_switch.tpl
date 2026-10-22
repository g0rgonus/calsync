% rebase('layout.tpl', title='Revoke every key', flash=flash, narrow=True)

<p class="eyebrow">Settings · emergency</p>
<h1>Revoke every key</h1>

% if error:
<p class="lede"><strong>{{ error }}</strong></p>
% end

% if step == 0:
<p><a class="btn btn-quiet" href="/settings#kill-switch">Back to settings</a></p>
% else:
<p class="note">Step {{ step }} of 5. Cancel at any step and nothing is revoked.</p>
<div class="card">
<form method="post" action="/settings/kill-switch">
  <input type="hidden" name="flow" value="{{ flow }}">
  <input type="hidden" name="step" value="{{ step }}">
% if step == 1:
  <p>Every key stops working at once: admin keys, team keys, the household read key,
     and the read API's token. Every tool that sends or reads events stops until you
     give it a new key. Calendars, teams and events are not touched.</p>
  <p>Use this when a key may have leaked and there is time to put new keys back.</p>
  <button class="btn" type="submit">I understand, continue</button>
% elif step == 2:
  <p>These {{ len(keys) }} keys will stop working:</p>
  <ul>
% for k in keys:
    <li>{{ k.label }} · {{ k.scope.replace('_', ' ') }}</li>
% end
    <li>the read API's token (from <span class="raw">.env</span>)</li>
  </ul>
  <p class="note">Feed teams keep syncing; they use no key.</p>
% if feeds:
  <label><input type="checkbox" name="pause_feeds" value="1"> Also pause the
    {{ len(feeds) }} feed {{ 'team' if len(feeds) == 1 else 'teams' }}</label>
% end
  <p><label><input type="checkbox" name="read" value="1" required> I have read this list</label></p>
  <button class="btn" type="submit">Continue</button>
% elif step == 3:
  <p>Type <span class="raw">{{ phrase }}</span> exactly.</p>
  <input name="phrase" autocomplete="off" required>
  <button class="btn" type="submit">Continue</button>
% elif step == 4:
  <p>Enter the admin password.</p>
  <input name="password" type="password" autocomplete="current-password" required>
  <button class="btn" type="submit">Continue</button>
% elif step == 5:
  <p><strong>Last chance.</strong> Wait {{ wait }} seconds, then press Revoke. Cancel
     stays here until then.</p>
  <button class="btn" type="submit">Revoke every key</button>
% end
</form>
<form method="post" action="/settings/kill-switch">
  <input type="hidden" name="flow" value="{{ flow }}">
  <input type="hidden" name="cancel" value="1">
  <button class="btn btn-quiet" type="submit">Cancel</button>
</form>
</div>
% end
