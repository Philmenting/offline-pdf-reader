def _vsep():
    return "|"  # Vertical separator for the toolbar layout


def _icon_btn(icon, tooltip):
    return f'<button class="icon-btn" title="{tooltip}">{icon}</button>'  # Button with icon


def _primary_btn(label):
    return f'<button class="primary-btn">{label}</button>'  # Primary action button


def _action_btn(label):
    return f'<button class="action-btn">{label}</button>'  # General action button


# Toolbar implementation
toolbar = [
    [
        _primary_btn('PDF öffnen'),  # File Operations
        _icon_btn('⏮', 'Zurück'),  # Navigation
        _icon_btn('◀', 'Vorwärts'),  
        _icon_btn('▶', 'Weiter'),  
        _icon_btn('⏭', 'Letzte Seite'),  
        _icon_btn('#', 'Seitenzahl'), 
        _icon_btn('−', 'Verkleinern'),  # Zoom
        _icon_btn('+', 'Vergrößern'),  
        _icon_btn('⊡', 'Fit zur Seite')  
    ],
    [
        _icon_btn('↺', 'Drehen links'),  # Rotation
        _icon_btn('↻', 'Drehen rechts'),  
        _icon_btn('⟲', 'Drehung zurücksetzen'),  
        _icon_btn('↶', 'Rückgängig'),  # Undo
        _icon_btn('↷', 'Wiederherstellen')  # Redo
    ],
    [
        _icon_btn('Seite erkennen'),  # OCR
        _icon_btn('Alle erkennen'),  
        _icon_btn('OCR + Benennen'),  
        _primary_btn('Speichern'),  # Save button
        _primary_btn('Speichern als')  
    ],
    [
        _primary_btn('Zusammenführen'),  # PDF Tools
        _primary_btn('Extrahieren'),  
        _primary_btn('Sortieren'),  
        _primary_btn('Leer entfernen'),  
        _icon_btn('🔍', 'Suche')  
    ]
]

# Display the toolbar in the application
for row in toolbar:
    print(" ".join(row))
