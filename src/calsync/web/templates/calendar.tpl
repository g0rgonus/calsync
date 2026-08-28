% rebase('layout.tpl', title=month.strftime('%B %Y'), flash=flash)

<p class="eyebrow">On the calendar</p>
<h1>{{ month.strftime('%B %Y') }}</h1>

<div class="cal-bar">
  <div class="cal-months">
    <a class="btn btn-quiet" href="{{ link(month=previous) }}" rel="prev">‹ {{ previous.strftime('%b') }}</a>
    <a class="btn btn-quiet" href="{{ link(month=today.replace(day=1)) }}">Today</a>
    <a class="btn btn-quiet" href="{{ link(month=following) }}" rel="next">{{ following.strftime('%b') }} ›</a>
  </div>

  <div class="cal-views">
    <a class="pill {{ 'pill-on' if mode == 'month' else '' }}" href="{{ link(mode='month') }}">Month</a>
    <a class="pill {{ 'pill-on' if mode == 'agenda' else '' }}" href="{{ link(mode='agenda') }}">Agenda</a>
  </div>
</div>

% if len(children) > 1:
<div class="cal-filter">
  <a class="pill {{ '' if child_id else 'pill-on' }}" href="{{ link(child=None) }}">Everyone</a>
% for kid in children:
  <a class="pill {{ 'pill-on' if child_id == kid.id else '' }}" href="{{ link(child=kid.id) }}">{{ kid.name }}</a>
% end
</div>
% end

% if not entries:
<div class="empty">
  <p><strong>Nothing written for {{ month.strftime('%B') }}.</strong></p>
% if month < horizon.replace(day=1):
  <p>This month is past the retention window — what calsync remembers is pruned
     to the last {{ (today - horizon).days }} days. The events themselves are
     still on the calendar server; only calsync's own record of them is gone.</p>
% else:
  <p>Either no team has an event this month, or nothing has been synced yet.</p>
% end
</div>

% elif mode == 'month':
<table class="cal">
  <thead>
    <tr>
% for label in ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'):
      <th>{{ label }}</th>
% end
    </tr>
  </thead>
  <tbody>
% for week in weeks:
    <tr>
%   for day in week:
%     outside = day.month != month.month
      <td class="{{ 'cal-out' if outside else '' }} {{ 'cal-today' if day == today else '' }}">
        <span class="cal-num">{{ day.day }}</span>
%     for entry in by_day.get(day, []):
        <a class="chip chip-c{{ entry['colour'] }} {{ 'chip-off' if entry['cancelled'] else '' }} {{ 'chip-held' if entry['held'] else '' }}"
           href="{{ link(mode='agenda') }}#{{ entry['anchor'] }}"
           title="{{ entry['title'] }}{{ ' · ' + entry['venue'].name if entry['venue'] and entry['venue'].name else '' }}">
          <b>{{ 'all day' if entry['all_day'] else entry['local'].strftime('%-H:%M') }}</b> {{ entry['title'] }}
        </a>
%     end
      </td>
%   end
    </tr>
% end
  </tbody>
</table>

% else:
<div class="agenda">
% shown = None
% for entry in entries:
%   if entry['day'] != shown:
%     shown = entry['day']
  <h2 class="agenda-day {{ 'agenda-today' if entry['day'] == today else '' }}">
    {{ entry['day'].strftime('%A %-d %B') }}
  </h2>
%   end
  <div class="agenda-row" id="{{ entry['anchor'] }}">
    <span class="agenda-time">{{ 'all day' if entry['all_day'] else entry['local'].strftime('%-H:%M') }}</span>
    <span class="agenda-bar chip-c{{ entry['colour'] }}"></span>
    <div class="agenda-body">
      <p class="agenda-title {{ 'struck' if entry['cancelled'] else '' }}">{{ entry['title'] }}</p>
      <p class="note">
% if entry['venue'] and entry['venue'].name:
        {{ entry['venue'].name }}{{ ' ' + entry['venue'].field if entry['venue'].field else '' }}
% elif entry['venue']:
        <span class="raw">{{ entry['venue'].raw }}</span>
% else:
        no venue given
% end
        · <a href="/sources/{{ entry['source_id'] }}">{{ entry['activity'].name }}</a>
        · {{ entry['child'].name }}
      </p>
% if entry['cancelled']:
      <p class="note"><span class="tag tag-off">cancelled</span>
         Left in place as a tombstone, which is how the removal reaches a phone
         that already has it.</p>
% elif entry['held']:
      <p class="note"><span class="tag tag-asking">held for review</span>
         In <span class="raw">{{ entry['collection'] }}</span> rather than a
         family calendar — calsync could not tell which one it belongs in.
         <a href="/review">Answer that</a> and it moves.</p>
% end
    </div>
  </div>
% end
</div>
% end

<p class="note" style="margin-top:1.4rem">
  What calsync wrote, not what the feeds currently say — the record is written
  after the calendar accepts each event, so this is what is on somebody's phone.
  Times are the venue's, {{ tz }} where a feed did not say otherwise. Anything
  added to the calendar by hand is not here: calsync never saw it, and does not
  touch it.
</p>
