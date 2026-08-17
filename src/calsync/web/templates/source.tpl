% rebase('layout.tpl', title=activity.name, flash=flash)

<p class="eyebrow">{{ child.name }} · {{ activity.sport }} · <span style="text-transform:none;letter-spacing:0">{{ source.id }}</span></p>
<h1>{{ activity.emoji }} {{ activity.name }}</h1>

% if report.status == 'error':
<div class="banner banner-bad">
  <strong>The feed did not come back.</strong>
  Nothing is known about the parse until it does, and nothing on the calendar has
  been touched — a failed fetch is never read as a cancelled season.
  <div class="raw-block" style="margin-top:0.7rem">{{ '; '.join(report.errors) }}</div>
</div>
% elif report.held:
<div class="banner banner-bad">
  <strong>A guard held this poll.</strong> {{ report.held }}
  Nothing was cancelled. Look at the feed before doing anything else.
</div>
% end

<div class="card">
  % include('_gate.tpl', conditions=conditions, big=True)
</div>

<div class="btn-row" style="margin-top:1.4rem">
% if source.staging_collection:
%   if state == 'ready':
  <form method="post" action="/sources/{{ source.id }}/promote">
    <button class="btn" type="submit">Promote to the real calendars</button>
  </form>
  <span class="note">Events relocate on the next sync. No duplicates, no cleanup.</span>
%   else:
  <form method="post" action="/sources/{{ source.id }}/promote">
    <input type="hidden" name="force" value="1">
    <button class="btn btn-quiet" type="submit">Promote anyway</button>
  </form>
  <span class="note">
    Staged to <span class="raw">{{ source.staging_collection }}</span>.
    {{ 'Promoting now skips the checks below.' }}
  </span>
%   end
% else:
  <span class="tag tag-live">live</span>
  <form method="post" action="/sources/{{ source.id }}/stage">
    <input type="hidden" name="collection" value="onboarding">
    <button class="btn btn-quiet" type="submit">Send back to staging</button>
  </form>
% end
</div>

% asking = [c for c in conditions if c.state == 'asking']
% waiting = [c for c in conditions if c.state == 'waiting']
% met = [c for c in conditions if c.state == 'met']

% if asking:
<h2>Your turn</h2>
<div class="stack">
% for condition in asking:
  <section class="question">
    <h3>{{ condition.headline }}</h3>
    <p>{{ condition.detail }}</p>

%   # Skipped for venues: each one gets its own form below, which names it.
%   if condition.items and condition.answer != 'venue':
    <ul class="raw-list">
%     for item in condition.items[:24]:
      <li><span class="raw">{{ item }}</span></li>
%     end
%     if len(condition.items) > 24:
      <li class="note">and {{ len(condition.items) - 24 }} more</li>
%     end
    </ul>
%   end

%   if condition.answer == 'alias':
%     if condition.suggestions:
    <p class="label">Which of these is your team?</p>
    <div class="btn-row" style="margin-bottom:1rem">
%       for suggestion in condition.suggestions:
      <form method="post" action="/sources/{{ source.id }}/alias">
        <input type="hidden" name="alias" value="{{ suggestion }}">
        <button class="btn btn-answer" type="submit">{{ suggestion }}</button>
      </form>
%       end
    </div>
%     end
    <form method="post" action="/sources/{{ source.id }}/alias">
      <label class="field" style="margin-bottom:0.6rem">
        <span class="label">Or type it exactly as the coach writes it</span>
        <input type="text" name="alias" class="mono" required autocomplete="off">
      </label>
      <button class="btn btn-answer" type="submit">Add this name</button>
    </form>
%   elif condition.answer == 'type':
%     for item in condition.items:
    <div class="btn-row" style="margin-bottom:0.6rem">
      <span class="raw" style="min-width:11rem">{{ item }}</span>
%       for kind in ('game', 'practice'):
      <form method="post" action="/sources/{{ source.id }}/event-type">
        <input type="hidden" name="label" value="{{ item }}">
        <input type="hidden" name="kind" value="{{ kind }}">
        <button class="btn btn-answer" type="submit">is a {{ kind }}</button>
      </form>
%       end
    </div>
%     end
%   elif condition.answer == 'venue':
%     for item in condition.items:
    <form method="post" action="/sources/{{ source.id }}/venue" class="card" style="margin-bottom:0.8rem">
      <input type="hidden" name="raw" value="{{ item }}">
      <p class="label" style="margin-bottom:0.6rem">Where is <span class="raw">{{ item }}</span>?</p>
      <div class="row">
        <label class="field" style="margin-bottom:0.7rem">
          <span class="label">It's another name for</span>
          <select name="existing">
            <option value="">— a new place —</option>
%         for venue in venues:
            <option value="{{ venue['canonical_name'] }}">{{ venue['canonical_name'] }}</option>
%         end
          </select>
        </label>
        <label class="field" style="margin-bottom:0.7rem">
          <span class="label">Or call it</span>
          <input type="text" name="name" value="{{ item }}" autocomplete="off">
        </label>
        <label class="field" style="margin-bottom:0.7rem">
          <span class="label">Address (optional)</span>
          <input type="text" name="address" autocomplete="off">
        </label>
      </div>
      <button class="btn btn-answer" type="submit">Save this place</button>
      <span class="note" style="margin-left:0.6rem">
        No pin is guessed. Without coordinates the location still reads fine —
        it just isn't tappable.
      </span>
    </form>
%     end
%   end
  </section>
% end
</div>
% end

% if waiting:
<h2>Waiting</h2>
<div class="stack">
% for condition in waiting:
  <section class="question question-waiting">
    <h3>{{ condition.headline }}</h3>
    <p style="margin-bottom:0">{{ condition.detail }}</p>
  </section>
% end
</div>
% end

% if met:
<h2>Cleared</h2>
<div class="stack">
% for condition in met:
  <section class="question question-met">
    <h3 style="font-size:1rem;margin-bottom:0.15rem">{{ condition.headline }}</h3>
    <p style="margin-bottom:0">{{ condition.detail }}</p>
  </section>
% end
</div>
% end

<h2>This poll</h2>
<div class="card">
  <p style="margin:0 0 0.8rem">
    Checked when this page loaded, not read from a cache — a verdict from last
    week says nothing about a feed that has since grown a schedule.
  </p>
  <div class="raw-block">{{ report.line() }}</div>
</div>

<h2>Recent polls</h2>
<div class="card">
% if not polls:
  <p class="note" style="margin:0">No polls recorded yet. The poller picks this
     up on its next pass, within {{ source.poll_interval_s // 60 }} minutes.</p>
% else:
  <table class="polls">
% for poll in polls:
    <tr>
      <td>{{ poll['started_at'] }}</td>
      <td class="st-{{ poll['status'] }}">{{ poll['status'] }}</td>
      <td>{{ poll['detail'] or '' }}</td>
    </tr>
% end
  </table>
% end
% if health and health['last_error']:
  <p class="note" style="margin-top:0.9rem">
    Last error {{ health['last_error_at'] }}:
  </p>
  <div class="raw-block">{{ health['last_error'] }}</div>
% end
</div>

<h2>The team</h2>
<div class="card">
  <p class="note" style="margin-top:0">
    The middle four are not cosmetic. Together with the names below they are what
    the parser matches a fixture against, so "U10DA TASL Match vs Beach FC"
    yields an opponent rather than nothing. Changing them re-parses the feed on
    the next poll — no re-fetch, no re-onboarding.
  </p>
  <form method="post" action="/activities/{{ activity.id }}">
    <input type="hidden" name="back" value="/sources/{{ source.id }}">
    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Name — appears in every title</span>
        <input type="text" name="name" value="{{ activity.name }}" required autocomplete="off">
      </label>
      <label class="field" style="margin-bottom:0.8rem;flex:0 1 6rem">
        <span class="label">Emoji</span>
        <input type="text" name="emoji" value="{{ activity.emoji or '' }}"
               style="text-align:center" autocomplete="off">
      </label>
    </div>

    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Official name</span>
        <input type="text" name="official_name" class="mono"
               value="{{ activity.official_name or '' }}" placeholder="U10DA" autocomplete="off">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Short name</span>
        <input type="text" name="short_name" class="mono"
               value="{{ activity.short_name or '' }}" autocomplete="off">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">League</span>
        <input type="text" name="league" class="mono"
               value="{{ activity.league or '' }}" placeholder="TASL" autocomplete="off">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Age group</span>
        <input type="text" name="age_group" class="mono"
               value="{{ activity.age_group or '' }}" placeholder="U10" autocomplete="off">
      </label>
    </div>

    <label class="field" style="margin-bottom:0.8rem">
      <span class="label">Home ground</span>
      <select name="home_venue_id">
        <option value="">— not set —</option>
% for venue in venues:
        <option value="{{ venue['id'] }}" {{ 'selected' if activity.home_venue == venue['canonical_name'] else '' }}>{{ venue['canonical_name'] }}</option>
% end
      </select>
      <span class="choice-note" style="margin-top:0.35rem">
        The only thing that can mark a game as away. Some feeds phrase every
        fixture as "vs" whoever is hosting, so without this nothing is ever
        marked <span class="raw">@</span> — and a guess would be worse.
      </span>
    </label>

    <div class="row">
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">Remind before a game</span>
        <input type="number" name="alarm_game_min" min="0" value="{{ activity.alarm_game_min }}">
      </label>
      <label class="field" style="margin-bottom:0.8rem">
        <span class="label">…and before a practice</span>
        <input type="number" name="alarm_practice_min" min="0"
               value="{{ activity.alarm_practice_min }}">
      </label>
      <div class="field" style="margin-bottom:0.8rem">
        <span class="label">&nbsp;</span>
        <span class="note">Minutes. Away games need travel time; practices are usually local.</span>
      </div>
    </div>

    <button class="btn" type="submit">Save {{ activity.name }}</button>
  </form>
</div>

<h2>The source</h2>
<div class="card">
  <table class="derived">
    <tr>
      <th>Feed</th>
      <td colspan="2"><span class="raw">{{ source.url_template or '—' }}</span></td>
    </tr>
    <tr>
      <th>Names for this team</th>
      <td colspan="2">
% if activity.aliases:
%   for alias in activity.aliases:
        <span class="raw">{{ alias }}</span>
%   end
% else:
        <span class="note">just “{{ activity.name }}”</span>
% end
      </td>
    </tr>
    <tr>
      <th>On the calendar</th>
      <td colspan="2">{{ tracked }} events ·
        last successful poll {{ (health and health['last_success_at']) or 'never' }}</td>
    </tr>
  </table>

  <div class="btn-row" style="margin-top:1.2rem">
    <form method="post" action="/sources/{{ source.id }}/enabled">
      <input type="hidden" name="enabled" value="{{ '0' if source.enabled else '1' }}">
      <button class="btn btn-quiet" type="submit">
        {{ 'Pause polling' if source.enabled else 'Resume polling' }}
      </button>
    </form>
% if source.enabled:
    <span class="note">Pausing stops fetches. Nothing already written is removed.</span>
% end
  </div>
</div>

<h2>End of season</h2>
<div class="card">
% if dormant.suspected:
  <div class="banner banner-info" style="margin-bottom:1rem">
    <strong>{{ dormant.headline }}</strong> {{ dormant.reason }}
% if not source.enabled:
    Polling is off.
% elif source.config.get('persists_across_seasons'):
    Polling continues, because this one is kept across seasons.
% end
  </div>
% end
  <p class="note">
    Rec teams are not renamed between seasons, they are replaced — so when this
    one finishes, its last few events are still sitting in the family's
    calendars. Retiring removes them and stops polling, in that order.
  </p>
  <p class="note">
    Not a delete. The source row and its record of what was written stay, which
    is what stops a reused event id from being picked up as new next season.
% if tracked:
    <strong>{{ tracked }}</strong> events would come off the calendar.
% else:
    Nothing is on the calendar, so this only stops the polling.
% end
  </p>
  <form method="post" action="/sources/{{ source.id }}/retire">
    <button class="btn btn-danger" type="submit">Retire {{ activity.name }}</button>
  </form>

% persists = source.config.get('persists_across_seasons')
  <hr class="rule" style="margin:1.4rem 0">
  <form method="post" action="/sources/{{ source.id }}/persists">
    <input type="hidden" name="persists" value="{{ '0' if persists else '1' }}">
    <button class="btn btn-quiet" type="submit">
      {{ 'Treat as a single season' if persists else 'Keep this one across seasons' }}
    </button>
    <span class="note" style="margin-left:0.6rem">
% if persists:
      Kept across seasons, so a quiet summer will never switch it off.
% else:
      Most teams are replaced each year, so after two months with nothing new
      calsync stops polling this feed. A club team that comes back each autumn
      should be kept instead.
% end
    </span>
  </form>
</div>
