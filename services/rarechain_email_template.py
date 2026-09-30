from __future__ import annotations

from html import escape


def render_rarechain_email(category_label: str, headline: str, subtext: str, hero_image_url: str, stats: list[dict], second_image_url: str, caption: str, body_html: str) -> str:
    stat_cells = "".join(f'<td style="padding:14px 0;width:{100 / max(len(stats), 1):.2f}%;"><div style="font-family:\'DM Mono\',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#77787B;">{escape(str(stat.get("label", "")))}</div><strong style="display:block;margin-top:4px;font-size:{20 if len(stats) == 1 else 13}px;color:#0B0B0B;">{escape(str(stat.get("value", "")))}</strong></td>' for stat in stats)
    return f'''<div style="margin:0;background:#FAFAF8;color:#0B0B0B;font-family:'DM Sans',Arial,sans-serif;line-height:1.5;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#FAFAF8;padding:28px 12px;"><tr><td align="center">
    <table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:100%;max-width:640px;background:#FFFFFF;border:1px solid #E4E3DF;">
      <tr><td style="background:#0B0B0B;padding:18px 28px;color:#FFFFFF;font-family:'Space Grotesk','DM Sans',Arial,sans-serif;font-size:20px;font-weight:700;letter-spacing:-.03em;">RARECHAIN<span style="color:#A1A1A1;">.</span></td></tr>
      <tr><td style="padding:28px 28px 12px;"><div style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#77787B;">{escape(category_label)}</div><h1 style="margin:12px 0 10px;font-family:'Space Grotesk','DM Sans',Arial,sans-serif;font-size:32px;line-height:1.08;letter-spacing:-.04em;color:#0B0B0B;">{escape(headline)}</h1><p style="margin:0;color:#55565A;font-size:15px;">{escape(subtext)}</p></td></tr>
      <tr><td style="padding:12px 28px 24px;"><img src="{escape(hero_image_url)}" alt="RareChain logistics operations" width="584" style="display:block;width:100%;height:auto;max-height:280px;object-fit:cover;border:1px solid #E4E3DF;" /></td></tr>
      <tr><td style="padding:0 28px 24px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #E4E3DF;border-bottom:1px solid #E4E3DF;"><tr>{stat_cells}</tr></table></td></tr>
      <tr><td style="padding:0 28px 24px;"><img src="{escape(second_image_url)}" alt="Connected warehouse operations" width="584" style="display:block;width:100%;height:auto;max-height:220px;object-fit:cover;border:1px solid #E4E3DF;" /><div style="padding-top:8px;font-size:11px;color:#77787B;">{escape(caption)}</div></td></tr>
      <tr><td style="padding:0 28px 28px;color:#55565A;font-size:14px;line-height:1.65;">{body_html}</td></tr>
      <tr><td style="padding:20px 28px;border-top:1px solid #E4E3DF;background:#FAFAF8;color:#55565A;font-size:13px;">Warm regards,<br /><strong style="color:#0B0B0B;">Martin Reyes</strong><br />RareChain Logistics Team<br />Rare Global Food Trading Corp.<br />Unit SF02 Santana Grove, Soreena Avenue corner Dr. A. Santos Avenue<br />San Antonio Paranaque City, Manila, Philippines<br />Mobile: +63 9171145694<br /><a href="mailto:martin@rareglobalfood.com" style="color:#0B0B0B;">martin@rareglobalfood.com</a> · <a href="http://www.rareglobalfood.com" style="color:#0B0B0B;">www.rareglobalfood.com</a></td></tr>
    </table>
  </td></tr></table>
</div>'''
