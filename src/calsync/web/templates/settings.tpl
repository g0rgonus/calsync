% rebase('layout.tpl', title='Settings', flash=flash, narrow=True)
% setdefault('check', None)

<p class="eyebrow">Instance configuration</p>
<h1>Settings</h1>
<p class="lede">
  Every one of these is a row in the database rather than a line of code, which
  is what lets another household deploy this without editing anything. Nothing
  here is specific to one family.
</p>

<h2>Where events go</h2>
<div class="card">
  <form method="post" action="/settings/calendar">
    <label class="field" style="margin-bottom:0.8rem">
      <span class="label">Write events to</span>
      <select name="target_kind">
% for kind in kinds:
        <option value="{{ kind }}" {{ 'selected' if settings.target_kind == kind else '' }}>{{ kind }}</option>
% end
      </select>
      <span class="choice-note" style="margin-top:0.35rem">
        <span class="raw">google</span> is registered and its payload builder is
        tested, but nothing implements Google's OAuth exchange yet, so choosing
        it will refuse with that reason rather than half-write a season.
        <span class="raw">ics_file</span> needs <span class="raw">--out</span>.
      </span>
    </label>

    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">CalDAV server</span>
        <input type="text" name="radicale_url" class="mono"
               value="{{ settings.radicale_url }}" autocomplete="off">
      </label>
      <label class="field" style="margin-bottom:0.8rem;flex:0 1 9rem">
        <span class="label">Username</span>
        <input type="text" name="radicale_user" value="{{ settings.radicale_user }}"
               autocomplete="off">
      </label>
    </div>
    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Password — stored in the secret store, never here</span>
        <input type="password" name="radicale_password" autocomplete="new-password"
               placeholder="{{ 'set — leave blank to keep it' if radicale_has_password else 'not set' }}">
      </label>
      <label class="field" style="margin-bottom:0.8rem;flex:0 1 12rem">
        <span class="label">Secret name</span>
        <input type="text" name="radicale_secret_ref" class="mono"
               value="{{ settings.radicale_secret_ref }}" autocomplete="off">
      </label>
    </div>

    <label class="field" style="margin-bottom:0.8rem">
      <span class="label">Which calendar an event lands in</span>
      <input type="text" name="collection_template" class="mono"
             value="{{ settings.collection_template }}" autocomplete="off">
      <span class="choice-note" style="margin-top:0.35rem">
        <span class="raw">{type}</span> gives games and practices ·
        <span class="raw">{child}</span> one per kid ·
        <span class="raw">{child}-{type}</span> both ·
        also <span class="raw">{sport}</span> and <span class="raw">{activity}</span>.
        Changing this relocates every existing event on the next sync rather than
        stranding it — a changed collection is a move, not an update.
      </span>
    </label>

    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Games are called</span>
        <input type="text" name="collection_game_label"
               value="{{ settings.collection_game_label }}" autocomplete="off">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Practices are called</span>
        <input type="text" name="collection_practice_label"
               value="{{ settings.collection_practice_label }}" autocomplete="off">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Default timezone</span>
        <input type="text" name="default_tz" class="mono"
               value="{{ settings.default_tz }}" autocomplete="off">
      </label>
    </div>

    <button class="btn" type="submit">Save calendar settings</button>
  </form>
</div>

<h2>How titles read</h2>
<div class="card">
  <p class="note" style="margin-top:0">
    The title is rendered at write time, never stored — so changing this
    re-renders every event on the next sync without re-fetching anything.
    Getting it wrong is cheap and reversible.
  </p>

  <div class="raw-block" style="margin-bottom:1.1rem">{{ sample }}</div>
  <p class="note" style="margin-top:-0.7rem;margin-bottom:1.1rem">
    ↑ a home game against Strikers, as it would appear right now
  </p>

  <form method="post" action="/settings/titles">
    <label class="field" style="margin-bottom:0.8rem">
      <span class="label">Title template</span>
      <input type="text" name="title_template" class="mono"
             value="{{ settings.title_template }}" autocomplete="off">
      <span class="choice-note" style="margin-top:0.35rem">
        <span class="raw">{kids}</span> <span class="raw">{emoji}</span>
        <span class="raw">{detail}</span> <span class="raw">{sport}</span>
        <span class="raw">{activity}</span> <span class="raw">{venue}</span>.
        Empty fields collapse, so a template never leaves a dangling separator.
      </span>
    </label>

    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Two kids render as</span>
        <select name="multi_kid_style">
% for style, note in (('initials', 'initials — P+J'), ('names', 'names — Patrick+James')):
          <option value="{{ style }}" {{ 'selected' if settings.multi_kid_style == style else '' }}>{{ note }}</option>
% end
        </select>
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">This many or more</span>
        <input type="number" name="all_kids_threshold" min="2"
               value="{{ settings.all_kids_threshold }}">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">…are called</span>
        <input type="text" name="all_kids_label" value="{{ settings.all_kids_label }}"
               autocomplete="off">
      </label>
    </div>

    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Home games</span>
        <input type="text" name="home_marker" class="mono"
               value="{{ settings.home_marker }}" autocomplete="off">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Away games</span>
        <input type="text" name="away_marker" class="mono"
               value="{{ settings.away_marker }}" autocomplete="off">
      </label>
    </div>
    <p class="note" style="margin-top:-0.5rem">
      Away is only ever marked when it is positively known. Some feeds phrase
      every fixture as "vs" regardless, so an undetermined game renders as home
      rather than guessing.
    </p>

    <button class="btn" type="submit">Save title settings</button>
  </form>
</div>

<h2>Safety</h2>
<div class="card">
  <p class="note" style="margin-top:0">
    Cancellation is only ever signalled by an event <em>disappearing</em> from a
    feed, which makes a truncated response look exactly like a cancelled season.
    These thresholds are what stands between that and a wiped family calendar:
    past them, calsync holds every deletion and logs it instead. Narrow them
    freely; the form will not let you widen them past the point of being off.
  </p>

  <form method="post" action="/settings/safety">
    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Hold if more than this share vanishes</span>
        <input type="text" name="max_disappearance_pct" class="mono"
               value="{{ settings.max_disappearance_pct }}" autocomplete="off">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">…or this many events</span>
        <input type="number" name="max_disappearance_count" min="1" max="25"
               value="{{ settings.max_disappearance_count }}">
      </label>
    </div>
    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Keep this many days of past events</span>
        <input type="number" name="sync_window_back_days" min="0" max="365"
               value="{{ settings.sync_window_back_days }}">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">…and this many ahead</span>
        <input type="number" name="sync_window_forward_days" min="1" max="1095"
               value="{{ settings.sync_window_forward_days }}">
      </label>
    </div>
    <button class="btn" type="submit">Save safety settings</button>
  </form>
</div>

<h2>Matrix</h2>
<div class="card">
  <div class="banner banner-info" style="margin-bottom:1.2rem">
    <strong>Nothing sends a Matrix message yet.</strong>
    The room described in <span class="raw">docs/MATRIX.md</span> is a design
    contract, not code that exists. These four values are stored and — more
    usefully — <em>checked against your homeserver</em>, so that whenever the bot
    is built it starts from a configuration that has already been accepted rather
    than one that was typed once and never tried.
  </div>

  <form method="post" action="/settings/matrix">
    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Homeserver</span>
        <input type="text" name="matrix_homeserver" class="mono"
               value="{{ matrix.homeserver }}" autocomplete="off"
               placeholder="https://matrix.example.org">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">User id</span>
        <input type="text" name="matrix_user_id" class="mono"
               value="{{ matrix.user_id }}" autocomplete="off"
               placeholder="@calsync:example.org">
      </label>
    </div>
    <label class="field" style="margin-bottom:0.8rem">
      <span class="label">Room</span>
      <input type="text" name="matrix_room_id" class="mono"
             value="{{ matrix.room_id }}" autocomplete="off"
             placeholder="!abcdef:example.org">
    </label>
    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Access token — stored in the secret store, never here</span>
        <input type="password" name="matrix_access_token" autocomplete="new-password"
               placeholder="{{ 'set — leave blank to keep it' if matrix_has_token else 'not set' }}">
      </label>
      <label class="field" style="margin-bottom:0.8rem;flex:0 1 12rem">
        <span class="label">Secret name</span>
        <input type="text" name="matrix_secret_ref" class="mono"
               value="{{ matrix.secret_ref }}" autocomplete="off">
      </label>
    </div>

    <div class="btn-row">
      <button class="btn" type="submit">Save Matrix settings</button>
    </div>
  </form>

  <form method="post" action="/settings/matrix/verify" style="margin-top:1rem">
    <button class="btn btn-answer" type="submit">Check these against the homeserver</button>
  </form>

% if check:
  <table class="derived" style="margin-top:1.2rem">
% for finding in check.findings:
    <tr>
      <th>{{ finding.label }}</th>
      <td style="width:5rem;color:{{ 'var(--go)' if finding.ok else 'var(--stop)' }}">
        {{ 'ok' if finding.ok else 'no' }}
      </td>
      <td>{{ finding.detail }}</td>
    </tr>
% end
  </table>
% if check.ok:
  <p class="note" style="margin-top:0.7rem;color:var(--go)">
    Your homeserver accepts all of this. Nothing uses it yet.
  </p>
% end
% end
</div>
