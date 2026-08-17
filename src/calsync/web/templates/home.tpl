% rebase('layout.tpl', title='Teams', flash=flash)

<p class="eyebrow">Season</p>
<h1>Teams</h1>

% asking = [c for c in cards if c['state'] == 'asking']
% if asking and len(asking) == len(cards):
<p class="lede">
  {{ 'This team needs' if len(cards) == 1 else 'Every team needs' }} an answer from you.
</p>
% elif asking:
<p class="lede">
  {{ len(asking) }} of {{ len(cards) }} {{ 'team needs' if len(asking) == 1 else 'teams need' }}
  an answer from you. The rest are running.
</p>
% elif cards:
<p class="lede">Nothing needs you. Feeds are polled on their own schedule.</p>
% end

% if not cards:
<div class="empty">
  <p><strong>No teams yet.</strong></p>
  <p>Paste the feed URL from a team app and calsync reads the rest out of it —
     the team name, the season, the venues.</p>
  <a class="btn" href="/onboard">Add a team</a>
</div>
% else:

<div class="stack">
% for card in cards:
%   source = card['source']
%   activity = card['activity']
  <a class="source source-{{ card['state'] if card['state'] in ('asking', 'waiting', 'down') else ('live' if not source.staging_collection else 'waiting') }}"
     href="/sources/{{ source.id }}">
    <div class="source-head">
      <h2 class="team">{{ activity.emoji }} {{ activity.name }}</h2>
      <span class="who">{{ card['child'].name }} · {{ activity.sport }}</span>
%     if not source.enabled:
      <span class="tag tag-off">paused</span>
%     elif card['state'] == 'down':
      <span class="tag tag-down">feed unreachable</span>
%     elif card['state'] == 'asking':
      <span class="tag tag-asking">{{ card['state_label'] }}</span>
%     elif source.staging_collection:
      <span class="tag tag-staged">staged</span>
%     else:
      <span class="tag tag-live">live</span>
%     end
    </div>

    <div class="source-foot">
      % include('_gate.tpl', conditions=card['conditions'])
      <div class="counts">
%       if card['report'] is None:
        <b>{{ card['tracked'] }}</b> events on the calendar<br>
        not checked
%       else:
        <b>{{ card['feed_events'] }}</b> events in the feed<br>
        {{ card['report'].fixtures_seen }}
        {{ 'game' if card['report'].fixtures_seen == 1 else 'games' }} ·
        {{ card['tracked'] }} on the calendar
%       end
      </div>
    </div>
  </a>
% end
</div>

% if not live:
<p class="note" style="margin-top:1rem">
  Feeds were not checked. <a href="/">Check them now</a>.
</p>
% else:
<p class="note" style="margin-top:1rem">
  Each feed was fetched to build this page.
  <a href="/?check=0">Skip the check</a> to load from stored state instead.
</p>
% end
% end

% if not children:
<hr class="rule">
<h2>First, who is this for</h2>
<div class="card">
  <p class="note">A feed says nothing about whose team it is, so calsync needs at
     least one kid on file before it can take one.</p>
  <form method="post" action="/children">
    <input type="hidden" name="next" value="/onboard">
    <div class="row">
      <label class="field">
        <span class="label">Name</span>
        <input type="text" name="name" required autocomplete="off">
      </label>
      <label class="field">
        <span class="label">Initial</span>
        <input type="text" name="initial" maxlength="2" placeholder="used in shared titles" autocomplete="off">
      </label>
    </div>
    <button class="btn" type="submit">Add</button>
  </form>
</div>
% end
