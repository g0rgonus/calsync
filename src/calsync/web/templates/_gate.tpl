% # The four promotion conditions as four discrete blocks.
% # Filled = met. Dashed = waiting, and waiting is fine. Orange = your turn.
% setdefault('big', False)
% setdefault('conditions', ())
<div class="gate {{ 'gate-big' if big else '' }}">
% if conditions:
%   for condition in conditions:
  <div class="pip pip-{{ condition.state }}">
    <div class="pip-bar" role="img" aria-label="{{ condition.label }}: {{ condition.state }}"></div>
    <span class="pip-label">{{ condition.label }}</span>
  </div>
%   end
% else:
%   for label in ('Team matched', 'Types known', 'Venues placed', 'Games seen'):
  <div class="pip pip-moot">
    <div class="pip-bar" role="img" aria-label="{{ label }}: not checked"></div>
    <span class="pip-label">{{ label }}</span>
  </div>
%   end
% end
</div>
