% rebase('layout.tpl', title='Confirm the team', flash=flash, narrow=True)

<p class="eyebrow">Read from the feed · nothing created yet</p>
<h1>{{ found.team_name or 'This feed has no name' }}</h1>
<p class="lede">
  {{ found.event_count }} events, {{ found.reads_as_games }} of them reading as games.
  Check what the feed said, then confirm three things.
</p>

% if found.warnings:
<div class="banner banner-info">
% for warning in found.warnings:
  {{ warning }}<br>
% end
</div>
% end

<h2>What the feed said</h2>
<div class="card">
<table class="derived">
% row = 0
% def delay():
%   global row
%   row += 1
%   return 'animation-delay:%dms' % (row * 45)
% end

  <tr class="reveal" style="{{ delay() }}">
    <th>Team name</th>
    <td class="val">{{ found.calendar_name or '—' }}</td>
    <td>{{ 'X-WR-CALNAME' if found.calendar_name else 'the feed sets no calendar name' }}</td>
  </tr>

  <tr class="reveal" style="{{ delay() }}">
    <th>Season</th>
    <td class="val">{{ found.season_start or '?' }} → {{ found.season_end or '?' }}</td>
    <td>earliest and latest start time</td>
  </tr>

  <tr class="reveal" style="{{ delay() }}">
    <th>Your team's name<br>in fixtures</th>
% if found.team_token:
    <td class="val"><span class="raw">{{ found.team_token }}</span></td>
    <td>on
      {{ [t.count for t in found.tokens if t.token == found.team_token][0] }} of
      {{ sum(1 for t in found.tokens) }} names seen across fixtures — the rest
      appear once or twice each, which is what opponents do</td>
% else:
    <td class="val">—</td>
    <td>the fixtures name an opponent but never your side, so this can't be read
        out of the feed. Leave it blank and add it later when it matters, or type
        it if you know it.</td>
% end
  </tr>

  <tr class="reveal" style="{{ delay() }}">
    <th>Reads as</th>
    <td class="val">{{ found.reads_as_games }} games · {{ found.reads_as_practices }} practices
% if found.reads_as_unclear:
      · {{ found.reads_as_unclear }} unclear
% end
    </td>
    <td>before any configuration; the real parse runs once this exists</td>
  </tr>

  <tr class="reveal" style="{{ delay() }}">
    <th>Feed type</th>
    <td class="val">{{ found.kind or 'not recognised' }}</td>
    <td><span class="raw">{{ found.prodid or 'no PRODID' }}</span></td>
  </tr>
</table>
</div>

<h2>Venues · {{ sum(1 for v in venues if not v.known) }} new</h2>
<div class="card">
% if not venues:
  <p class="note" style="margin:0">This feed names no places at all.</p>
% else:
  <p class="note">
    Venues outlast teams. Anything already known costs nothing this season or any
    season after; the new ones can wait until the source page asks about them.
  </p>
  <table class="derived">
% for venue in venues:
    <tr>
      <th style="width:auto"><span class="raw">{{ venue.name }}</span></th>
      <td>{{ venue.count }}×{{ (' · fields ' + ', '.join(venue.fields)) if venue.fields else '' }}</td>
      <td>{{ 'known' if venue.known else 'new' }}</td>
    </tr>
% end
  </table>
% end
</div>

<h2>How the coach writes them</h2>
<div class="card">
  <p class="note">
    There is no feed format — coaches type these by hand, and the convention is
    whatever one person settled on. This is what yours does.
  </p>
  <ul class="raw-list">
% for text, count in found.summaries:
    <li><span class="raw">{{ text }}</span>{{ (' ×%d' % count) if count > 1 else '' }}</li>
% end
  </ul>
</div>

<hr class="rule">

<form method="post" action="/onboard/create">
  <input type="hidden" name="url" value="{{ url }}">
  <input type="hidden" name="season_start" value="{{ found.season_start or '' }}">
  <input type="hidden" name="season_end" value="{{ found.season_end or '' }}">

  <h2 style="margin-top:0">Confirm</h2>
  <div class="card">
    <label class="field">
      <span class="label">Team name — what shows up in the calendar</span>
      <input type="text" name="team_name" required autocomplete="off"
             value="{{ found.team_name or '' }}">
    </label>

    <div class="row">
      <label class="field">
        <span class="label">Whose team</span>
        <select name="child" required>
          <option value="">choose…</option>
% for child in children:
          <option value="{{ child.id }}">{{ child.name }}</option>
% end
        </select>
      </label>

      <label class="field">
        <span class="label">Sport</span>
        <select name="sport" required>
          <option value="">choose…</option>
% for sport in sports:
          <option value="{{ sport['id'] }}">{{ sport['emoji'] }} {{ sport['name'] }}</option>
% end
        </select>
      </label>
    </div>

    <p class="note" style="margin-top:-0.4rem">
      If this kid had a team in this sport before, its timezone, league, age
      group and alarm timings carry over — a new season is these two fields, not
      a dozen.
    </p>
  </div>

  <h2>Details</h2>
  <div class="card">
    <label class="field">
      <span class="label">Name in fixtures — becomes an alias</span>
      <input type="text" name="token" class="mono" autocomplete="off"
             value="{{ found.team_token or '' }}"
             placeholder="the exact string that appears in 'X vs Y'">
      <span class="choice-note" style="margin-top:0.35rem">
        This is what resolves a fixture into an opponent and a home/away marker.
        Getting it wrong produces no opponent rather than a wrong one, and the
        source page will tell you.
      </span>
    </label>

    <div class="row">
      <label class="field">
        <span class="label">Feed type</span>
        <select name="kind" required>
% for kind in kinds:
          <option value="{{ kind }}" {{ 'selected' if kind == found.kind else '' }}>{{ kind }}</option>
% end
        </select>
      </label>
      <label class="field">
        <span class="label">Timezone</span>
% if len(tz_choices) > 20:
        <select name="tz" class="mono" required>
%   for name in tz_choices:
          <option value="{{ name }}" {{ 'selected' if name == default_tz else '' }}>{{ name }}</option>
%   end
        </select>
% else:
        <input type="text" name="tz" class="mono" value="{{ default_tz }}" required>
% end
      </label>
      <label class="field">
        <span class="label">Check every</span>
        <select name="poll_interval_s">
          <option value="1200" selected>20 minutes</option>
          <option value="3600">an hour</option>
          <option value="21600">6 hours</option>
        </select>
      </label>
    </div>
  </div>

  <h2>The URL</h2>
  <div class="card">
% if plan.recommended == 'none':
    <p class="note">
      {{ plan.reason }}, so it is stored as it is. If part of it turns out to be
      a token, move it to the secret store by hand and put
      <span class="raw">{{ '{{secret:ref}}' }}</span> in its place.
    </p>
% else:
    <p class="note">
      A feed URL is a bearer credential: whoever holds it can read this child's
      schedule and the places they will be. Ticked parts go to the secret store
      and the source row keeps only a placeholder, so it stays safe to read,
      export and paste into a bug report.
    </p>
% end
% for part in plan.parts:
    <label class="choice">
      <input type="checkbox" name="vault" value="{{ part.key }}"
             {{ 'checked' if part.key in plan.recommended_keys else '' }}>
      <span class="choice-body">
        <span class="choice-title">{{ part.label }}</span>
        <span class="choice-note">
          <span class="raw">{{ part.preview }}</span>
          {{ ' — reads like a credential' if part.suspect else '' }}
        </span>
      </span>
    </label>
% end
  </div>

  <div class="btn-row" style="margin-top:1.5rem">
    <button class="btn" type="submit">Create and stage</button>
    <a class="btn btn-quiet" href="/onboard">Start over</a>
  </div>
  <p class="note" style="margin-top:0.8rem">
    Goes to the <span class="raw">onboarding</span> calendar, not to the family's
    real ones. Nothing moves until you promote it.
  </p>
</form>
