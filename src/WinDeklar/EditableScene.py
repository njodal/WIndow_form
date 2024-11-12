import copy
import math
import functools

import QTAux as qt
import WindowForm as wf
import WinDeklar.yaml_functions as yf

from PyQt5.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QUndoStack, QShortcut, \
    QGraphicsOpacityEffect, QGraphicsItem, QGraphicsLineItem, QGraphicsPolygonItem, QGraphicsItemGroup, \
    QGraphicsRectItem, QUndoCommand
from PyQt5.QtCore import QRectF, Qt, QPointF, QLineF
from PyQt5.QtGui import QPen, QColor, QPolygonF, QKeySequence, QTransform, QBrush


class EditableFigure(QGraphicsView):
    name_key    = 'name'
    type_key    = 'type'
    items_key   = 'items'
    item_key    = 'item'
    color_key   = 'color'
    alpha_key   = 'alpha'
    widgets_key = 'widgets'
    widget_key  = 'widget'

    def __init__(self, parent, config, multiple_selection=True, scale_factor=100,
                 edit_panel_name='input_panel_template.yaml'):
        """
        A figure that have items inside that can be editables (move, change size, etc)
        :param parent: main window
        :param config: config options :type Dict
        :param multiple_selection: whether allow selecting many options at once
        :param scale_factor: factor to relate pixels to meters
        :param edit_panel_name: name of the template for edit panel configuration
        """
        super().__init__()
        self.name   = config.get(self.name_key, 'no_name')
        self.parent = parent

        self.metadata       = get_metadata(self.parent)
        self.items_metadata = self.metadata.get(EditableFigure.items_key, [])
        self.props_metadata = self.metadata.get('properties', [])
        self.edit_template  = yf.get_yaml_file(edit_panel_name, directory=None)

        self.scene    = QGraphicsScene(self)
        self.setScene(self.scene)

        # undo functionality
        self.undo_stack = QUndoStack()
        undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        undo_shortcut.activated.connect(self.undo)

        redo_shortcut = QShortcut(QKeySequence("Ctrl+Y"), self)
        redo_shortcut.activated.connect(self.redo)

        # copy paste
        self.copy_buffer = None
        copy_shortcut = QShortcut(QKeySequence("Ctrl+C"), self)
        copy_shortcut.activated.connect(self.on_copy)

        paste_shortcut = QShortcut(QKeySequence("Ctrl+V"), self)
        paste_shortcut.activated.connect(self.on_paste)

        # selection
        if multiple_selection:
            self.setDragMode(QGraphicsView.RubberBandDrag)

        self.scale_factor = scale_factor
        self.scale(1, -1)  # to point y-axis up, not down

        # zoom parameters
        self.zoom_factor  = 1.15
        self.min_zoom     = 0.1
        self.max_zoom     = 10.0
        self.current_zoom = 1.0

    def get_items(self):
        return [item.serialize() for item in self.items() if isinstance(item, SceneItem)]

    def get_pos_in_scene(self, pos_in_view):
        return self.mapToScene(pos_in_view)

    def get_item_in_position(self, pos_in_view):
        pos_in_scene = self.get_pos_in_scene(pos_in_view)
        for item1 in self.scene.items():
            item = item1.group()
            if item is None:
                continue
            if item.contains(pos_in_scene):
                return item
        return None

    def get_metadata_for_type(self, item_type):
        for item1 in self.items_metadata:
            item = item1[EditableFigure.item_key]
            if item[EditableFigure.type_key] == item_type:
                return item
        return None

    def add_item(self, item_def):
        if EditableFigure.item_key not in item_def:
            msg = 'Invalid items definition format, %s not present in %s' % (EditableFigure.item_key, item_def)
            return msg

        item, msg = SceneItem.create(item_def[EditableFigure.item_key], self)
        if item is None:
            return msg
        self.scene.addItem(item)
        return ''

    def add_items(self, items_def, points_box=None):
        """
        Adds a set of items to a scene
        :param items_def:
        :param points_box: bounding box of all items :type PointsBox
        :return:
        """
        if EditableFigure.items_key not in items_def:
            msg = 'Invalid items definition format, group %s not present in %s' % (EditableFigure.items_key, items_def)
            return [msg]

        if points_box is not None:
            self.scene.setSceneRect(rect_from_points_box(points_box))

        fails_msg = []
        for item_def in items_def[EditableFigure.items_key]:
            fail_msg = self.add_item(item_def)
            if fail_msg != '':
                fails_msg.append(fail_msg)
        return fails_msg

    def add_item_from_ui(self, item_type, position):
        """
        Add an item from in a given position
        :param item_type:
        :param position:
        :return:
        """
        item_def  = get_default_item(item_type, self.get_metadata_for_type(item_type))
        item, msg = SceneItem.create(item_def, self)
        if item is None:
            return
        item.translate(position)
        self.add_ui_command(AddItemCommand(self, item))

    def delete_item_from_ui(self, item_in_position):
        """
        Delete the item in a given position
        :param item_in_position:
        :return:
        """
        if item_in_position is None:
            return
        self.add_ui_command(RemoveItemCommand(self, item_in_position))

    def remove_item(self, item):
        if isinstance(item, SceneItem):
            item.remove_handles()
        self.scene.removeItem(item)

    def delete_selected_items(self):
        for item in self.scene.selectedItems():
            self.remove_item(item)

    def clear(self):
        self.add_ui_command(RemoveItemsCommand(self, self.scene.items()))

    def remove_handles(self, non_check_handle=None):
        for handle in self.scene.items():
            if not isinstance(handle, Handle) or handle == non_check_handle:
                continue
            self.scene.removeItem(handle)
            del handle  # just to free memory
        self.update_scene()

    def selected_items(self):
        for item in self.view.scene.selectedItems():
            yield item

    def set_visible_type(self, item_type, value):
        for item in self.items():
            if not isinstance(item, SceneItem):
                continue
            if item.type == item_type:
                item.setVisible(value)

    def event_pos_to_point(self, event_pos):
        """
        Returns the corresponding point (in drawing reference frame) from a pixel point in view
        :param event_pos:
        :return:
        """
        translate = QPointF(0, 0)
        return pixel_point_to_point(self.mapToScene(event_pos), self.scale_factor, translate)

    # undo
    def undo(self):
        self.undo_stack.undo()

    def redo(self):
        self.undo_stack.redo()

    def add_ui_command(self, command):
        """
        Adds a UI command (like add or translate) that can be undone it
        :param command:
        :return:
        """
        self.undo_stack.push(command)

    # copy paste
    def on_copy(self):
        cursor_pos = self.mapFromGlobal(self.cursor().pos())
        scene_item = self.get_item_in_position(cursor_pos)
        self.copy_item(scene_item)

    def on_paste(self):
        cursor_pos = self.mapFromGlobal(self.cursor().pos())
        scene_pos  = self.get_pos_in_scene(cursor_pos)
        self.paste_item(scene_pos)

    def copy_item(self, scene_item):
        self.copy_buffer = scene_item

    def paste_item(self, scene_pos):
        """
        Paste the copied item in a given scene position
        :param scene_pos:
        :return:
        """
        if self.copy_buffer is None:
            return
        command = CopyPasteCommand(self, self.copy_buffer, scene_pos)
        self.add_ui_command(command)

    # events
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            selected_item = self.get_item_in_position(event.pos())
            if isinstance(selected_item, SceneItem):
                selected_item.set_handles()
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        """
        Capture mouse wheel to zoom in or out
        :param event:
        :return:
        """
        if event.angleDelta().y() > 0:
            # Zoom in
            self.zoom_in()
        else:
            # Zoom out
            self.zoom_out()

    def contextMenuEvent(self, event):
        """
        Display a context menu
        :param event:
        :return:
        """
        actions = []
        # add actions
        for item_metadata in self.items_metadata:
            item = item_metadata[EditableFigure.item_key]
            if 'default' not in item:
                # only include item who have default
                continue
            item_type = item['type']
            pos_in_scene = self.get_pos_in_scene(event.pos())
            add_event    = functools.partial(self.add_item_from_ui, item_type, pos_in_scene)
            actions.append(['Add %s' % item_type, add_event])

        item_in_position = self.get_item_in_position(event.pos())

        # copy paste actions
        copy_paste_actions = self.get_copy_paste_actions(item_in_position, event.pos())
        if len(actions) > 0 and len(copy_paste_actions) > 0:
            actions.append(['Separator', None])
        actions.extend(copy_paste_actions)

        if item_in_position is not None:
            # edit and delete actions
            actions.append(['Separator', None])
            type_and_name = item_in_position.type_and_name()
            actions.append(['Edit %s' % type_and_name,
                            functools.partial(edit_item_from_ui, item_in_position)])
            actions.append(['Delete %s' % type_and_name,
                            functools.partial(self.delete_item_from_ui, item_in_position)])

        context_menu = qt.Menu(self, actions=actions)
        context_menu.popup()

    def get_copy_paste_actions(self, item_in_position, event_pos):
        actions = []
        # copy
        if item_in_position is not None:
            actions.append(['Copy %s' % item_in_position.type_and_name(),
                            functools.partial(self.copy_item, item_in_position)])
        # paste
        if self.copy_buffer is not None:
            actions.append(['Paste %s' % self.copy_buffer.type_and_name(),
                            functools.partial(self.paste_item, self.get_pos_in_scene(event_pos))])
        return actions

    # zoom
    def zoom_in(self):
        if self.current_zoom * self.zoom_factor < self.max_zoom:
            self.scale(self.zoom_factor, self.zoom_factor)
            self.current_zoom *= self.zoom_factor

    def zoom_out(self):
        if self.current_zoom / self.zoom_factor > self.min_zoom:
            self.scale(1 / self.zoom_factor, 1 / self.zoom_factor)
            self.current_zoom /= self.zoom_factor

    # update figure
    def update_scene(self):
        self.update()

    def update_figure(self):
        """
        Initialize the scene with items from provider
        :return:
        """
        self.items().clear()
        self.parent.provider.update_view(self, None)  # calls provider to get the initial items


class SceneItem(QGraphicsItemGroup):
    """
    Base class for any item in a scene, provides all the common functionality

    """
    is_movable_key    = 'is_movable'
    is_selectable_key = 'is_selectable'
    start_key   = 'start'
    end_key     = 'end'
    center_key  = 'center'
    radius_key  = 'radius'
    tooltip_key = 'tooltip'
    width_key   = 'width'
    height_key  = 'height'

    @staticmethod
    def create(item_def, view):
        """
        Create a SceneItem from its dict definition
        :param item_def:
        :param view:
        :return: an SceneItem and a msg (in case the definition is wrong or incomplete)
        """
        if EditableFigure.type_key not in item_def:
            return None, '%s not present in %s, ignored' % (EditableFigure.type_key, item_def)
        item_type = item_def[EditableFigure.type_key]
        metadata = view.get_metadata_for_type(item_type)
        if metadata is None:
            return None, '%s type is not implemented' % item_type

        fail_msg  = check_must_have_properties(item_def, metadata)
        if fail_msg != '':
            return None, fail_msg

        constructor_name = metadata['constructor']
        if constructor_name in globals() and callable(globals()[constructor_name]):
            return globals()[constructor_name](item_def, view), ''
        else:
            return None, '%s constructor name not implemented' % constructor_name

    def __init__(self, item_def, view):
        """
        Super class to define an item that can be
        :param item_def:  item definition :type dict
        :param view: view that host the item
        """
        # to avoid warnings
        self.name  = ''
        self.type  = ''
        self.color = ''
        self.alpha = None
        self.width = 0.1

        self.pen          = QPen()
        self.view         = view
        self.scale_factor = self.view.scale_factor  # keep the scale factor used in creation time

        super().__init__()

        self.item_def = item_def
        self.update_state()

        # flags
        if self.item_def.get(self.is_movable_key, False):
            self.setFlag(QGraphicsItem.ItemIsMovable)
        if self.item_def.get(self.is_selectable_key, False):
            self.setFlag(QGraphicsItem.ItemIsSelectable)
        self.setAcceptHoverEvents(True)

        self.handles = []  # handles are created only when the item is clicked

    def update_state(self):
        self.name  = self.item_def.get(EditableFigure.name_key, '')
        self.type  = self.item_def.get(EditableFigure.type_key, '')
        self.color = self.item_def.get(EditableFigure.color_key, None)
        self.alpha = self.item_def.get(EditableFigure.alpha_key, 5)
        self.width = scale(self.item_def.get(self.width_key, 0.01), self.scale_factor)

        tooltip = self.item_def.get(self.tooltip_key, self.name)
        if tooltip != '':
            self.setToolTip(tooltip)

        self.set_color()
        self.set_border_width()

        if self.alpha is not None:
            opacity_effect = QGraphicsOpacityEffect()
            opacity_effect.setOpacity(self.alpha/10)
            self.setGraphicsEffect(opacity_effect)

    def set_pen(self):
        """
        Abstract method, should in implemented in subtypes
        :return:
        """
        pass

    def set_color(self):
        if self.color is None or self.color == '':
            return
        self.pen.setColor(QColor(self.color))

    def set_border_width(self):
        self.pen.setCapStyle(Qt.FlatCap)
        self.pen.setWidth(self.width)

    def clone(self):
        new_def       = copy.deepcopy(self.serialize())
        new_item, msg = self.create(new_def, self.view)
        return new_item

    # handles
    def set_handles(self):
        self.handles = self.get_handles()

    def remove_handles(self, non_check_handle=None):
        if self.view is None:
            return
        self.view.remove_handles(non_check_handle=non_check_handle)

    def get_handles(self):
        return []

    def end_resizing(self):
        """
        All housekeeping after resizing is finished
        :return:
        """
        self.remove_handles()

    def edit(self):
        """
        Displays a dialog to edit the properties (using the undo feature)
        :return:
        """
        dialog_full_name    = None  # dialog is built on the fly, do not use external definition
        editable_properties = self.get_editable_properties()
        dialog_config       = self.get_edit_dialog_config(editable_properties)
        properties_window   = wf.PropertiesHost(dialog_full_name, editable_properties, dialog_config=dialog_config)
        changed             = properties_window.show()
        command             = ChangePropertiesCommand(self, changed)
        self.view.add_ui_command(command)

    def update_properties(self, new_properties):
        # print('new properties: %s' % new_properties)
        self.item_def.update(new_properties)
        self.update_state()  # sync internal state
        self.set_pen()       # to reflect visual changes (like new color or alpha)

    def get_editable_properties(self):
        properties = {prop_name: self.item_def.get(prop_name, None) for prop_name
                      in self.view.get_metadata_for_type(self.type).get('editable_properties', [])}
        return properties

    def get_edit_dialog_config(self, editable_properties):
        dialog_config = self.view.edit_template.copy()
        # add specific properties
        it_key  = EditableFigure.item_key
        widgets = dialog_config['window']['layout'][0][it_key]['layout'][0][it_key][EditableFigure.widgets_key]
        widgets.clear()
        for k in editable_properties:
            for p1 in self.view.props_metadata:
                p = p1['property']
                if k == p[EditableFigure.name_key]:
                    widget_def = {EditableFigure.widget_key: p}
                    widgets.append(widget_def)
        return dialog_config

    # events
    def hoverEnterEvent(self, event):
        # print('mouse on %s' % self.name)
        super().hoverEnterEvent(event)

    # serialization
    def serialize(self):
        self.update_def_from_scene()
        return self.item_def

    def update_def_from_scene(self):
        """
        Update item definition with scene info, typically used for updating position or size
        :return:
        """
        pass

    def type_and_name(self):
        return '%s %s' % (self.type, self.name)


class SceneLine(SceneItem):
    """
    Represent a line
    """

    def __init__(self, item_def, view):
        """
        Define a line
        """
        super().__init__(item_def, view)
        start_point        = item_def[SceneItem.start_key]
        end_point          = item_def[SceneItem.end_key]
        start_point_pixels = point_to_pixel_point(start_point, self.scale_factor)
        end_point_pixels   = point_to_pixel_point(end_point, self.scale_factor)
        original_line      = QLineF(start_point_pixels, end_point_pixels)
        self.line          = QGraphicsLineItem(original_line)
        self.set_pen()
        self.addToGroup(self.line)

    def set_pen(self):
        self.line.setPen(self.pen)

    def contains(self, point: QPointF):
        d = distance_to_segment(self.p1(), self.p2(), point)
        return d < self.contain_width()

    def contain_width(self):
        return 10

    def p1(self):
        return self.line.line().p1()

    def p2(self):
        return self.line.line().p2()

    def center_pixel_point(self):
        return middle_pixel_point(self.p1(), self.p2())

    def start_point(self):
        return pixel_point_to_point(self.p1(), self.scale_factor, self.pos())

    def end_point(self):
        return pixel_point_to_point(self.p2(), self.scale_factor, self.pos())

    def length_in_pixels(self):
        """
        Returns the line length in pixels
        :return:
        """
        return distance_in_pixels(self.p1(), self.p2())

    # update
    def update_line_end_point(self, is_start, new_end_point_pos):
        """
        Update one of the line end points depending on the is_start flag
        :param is_start: if True update start end point, else the other one
        :param new_end_point_pos:
        :return:
        """
        p1, p2 = [new_end_point_pos, self.p2()] if is_start else [self.p1(), new_end_point_pos]
        self.line.setLine(QLineF(p1, p2))
        self.update_others()

    def translate(self, translation):
        p1, p2 = [translate_pixel_point(p, translation) for p in [self.p1(), self.p2()]]
        self.line.setLine(QLineF(p1, p2))
        self.update_others()

    def update_others(self):
        """
        Updates other item depending on the main line (to be implemented in subtypes like corridor)
        :return:
        """
        pass

    # handles
    def get_handles(self):
        handle_start      = ChangeEndPointHandle(self, True)     # enlarge start point
        handle_end        = ChangeEndPointHandle(self, False)    # enlarge end point
        handle_start_move = RotateHandle(self, True)      # rotate around end point
        handle_end_move   = RotateHandle(self, False)     # rotate around start point
        handle_move       = MoveHandle(self)              # move the whole item
        handles = [handle_start, handle_end, handle_start_move, handle_end_move, handle_move]
        return handles

    def update_def_from_scene(self):
        """
        Update item definition with scene info
        :return:
        """
        self.item_def[self.start_key] = self.start_point()
        self.item_def[self.end_key]   = self.end_point()

    def __str__(self):
        return 'line %s,%s' % (self.start_point(), self.end_point())


class SceneCorridor(SceneLine):
    """
    A movable, resizable corridor (a center line with two borders)
    """
    def __init__(self, item_def, view):
        super().__init__(item_def, view)
        self.corridor_width = scale(item_def.get('corridor_width', 1.0), self.scale_factor)
        self.center_line    = QGraphicsLineItem(self.line.line())
        center_line_pen     = QPen()
        center_line_color   = QColor('black')
        center_line_color.setAlpha(255)
        center_line_pen.setColor(center_line_color)
        center_line_pen.setStyle(Qt.DashLine)
        center_line_pen.setWidth(1)
        self.center_line.setPen(center_line_pen)
        self.addToGroup(self.center_line)

        self.border1 = QGraphicsLineItem()
        self.border2 = QGraphicsLineItem()

        self.update_borders()
        self.addToGroup(self.border1)
        self.addToGroup(self.border2)

    def set_pen(self):
        self.line.setPen(self.pen)

    def contain_width(self):
        return self.corridor_width

    def get_borders_lines(self):
        p1, p2  = pixel_points_to_point([self.p1(), self.p2()])
        borders = parallel_segments(p1, p2, self.corridor_width/2)
        lines   = [QLineF(point_to_pixel_point(start, 1.0), point_to_pixel_point(end, 1.0)) for [start, end] in borders]
        return lines

    # update
    def update_others(self):
        self.center_line.setLine(self.line.line())
        self.update_borders()

    def update_borders(self):
        """
        Make borders consistent with central line
        :return:
        """
        if not self.item_def.get('show_borders', False):
            return
        lines = self.get_borders_lines()
        self.border1.setLine(lines[0])
        self.border2.setLine(lines[1])

    def __str__(self):
        return 'corridor %s,%s' % (self.start_point(), self.end_point())


class SceneCircle(SceneItem):
    def __init__(self, item_def, view):
        """
        Define a circle from a center point and radius
        """
        super().__init__(item_def, view)
        center_point  = item_def[SceneItem.center_key]
        radius        = item_def[SceneItem.radius_key]
        center_pixels = point_to_pixel_point(center_point, self.scale_factor)
        radius_pixels = scale(radius, self.scale_factor)
        self.circle   = get_circle(center_pixels, radius_pixels)
        self.set_pen()
        self.addToGroup(self.circle)

    def set_pen(self):
        self.circle.setPen(self.pen)

    def contains(self, point: QPointF):
        d = distance_in_pixels(self.center_pixel_point(), point)
        return d < self.radius_pixels()

    def center_pixel_point(self):
        """
        Returns the circle's center in pixels
        :return:
        """
        bounding_rect = self.circle.rect()
        return bounding_rect.center()

    def radius_pixels(self):
        bounding_rect = self.circle.rect()
        return bounding_rect.width() / 2

    def center(self):
        """
        Returns the circle's center in meters
        :return:
        """
        return pixel_point_to_point(self.center_pixel_point(), self.scale_factor, self.pos())

    def radius(self):
        return de_scale(self.radius_pixels(), self.scale_factor)

    # update
    def translate(self, translation):
        bounding_rect = self.circle.rect()
        self.circle.setRect(bounding_rect.x() + translation.x(), bounding_rect.y() + translation.y(),
                            bounding_rect.width(), bounding_rect.height())

    def update_size(self, new_radius):
        """
        Changes the circle radius (new radius is from center to new_position)
        :param new_radius:
        :return:
        """
        # print(center, new_position, new_radius)
        center = self.center_pixel_point()
        self.circle.setRect(center.x() - new_radius, center.y() - new_radius, new_radius*2, new_radius*2)

    # handles
    def get_handles(self):
        handle_enlarge = ChangeSizeHandle(self)
        handle_move    = MoveHandle(self)              # move the whole item
        handles = [handle_enlarge, handle_move]
        return handles

    def update_def_from_scene(self):
        """
        Update item definition with scene info
        :return:
        """
        self.item_def[self.center_key] = self.center()
        self.item_def[self.radius_key] = self.radius()

    def __str__(self):
        return 'circle %s,%s' % (self.center(), self.radius())


class SceneRectangle(SceneItem):
    def __init__(self, item_def, view):
        """
        Define a rectangle from a center, width, height and rotation
        """
        self.rectangle    = None  # to avoid crash in set_color
        self.border_width = item_def.get('border_width', 1)
        super().__init__(item_def, view)
        self.rectangle = self.get_rectangle()
        self.set_pen()
        self.addToGroup(self.rectangle)

    def get_rectangle(self):
        center_point  = self.item_def[SceneItem.center_key]
        center_pixels = point_to_pixel_point(center_point, self.scale_factor)
        rotation      = self.item_def.get('rotation', 0)
        width_pixels  = scale(self.item_def.get(SceneItem.width_key, 1), self.scale_factor)
        height_pixels = scale(self.item_def.get(SceneItem.height_key, 1), self.scale_factor)
        return get_rectangle(center_pixels, width_pixels, height_pixels, rotation)

    def set_pen(self):
        self.set_color()
        self.rectangle.setPen(self.pen)

    def set_color(self):
        if self.rectangle is None or self.color is None or self.color == '':
            return
        brush = QBrush(QColor(self.color))
        self.rectangle.setBrush(brush)

    def set_border_width(self):
        self.pen.setWidth(self.border_width)

    def contains(self, point: QPointF):
        local_point = self.rectangle.mapFromScene(point)
        return self.rectangle.rect().contains(local_point)

    def center_pixel_point(self):
        """
        Returns the circle's center in pixels
        :return:
        """
        local_center = self.rectangle.rect().center()
        scene_center = self.rectangle.mapToScene(local_center)
        return scene_center

    def center(self):
        """
        Returns the circle's center in meters
        :return:
        """
        return pixel_point_to_point(self.center_pixel_point(), self.scale_factor, self.pos())

    # update
    def translate(self, translation):
        bounding_rect = self.rectangle.rect()
        self.rectangle.setRect(bounding_rect.x() + translation.x(), bounding_rect.y() + translation.y(),
                               bounding_rect.width(), bounding_rect.height())

    def update_width(self, new_width):
        pass

    def update_height(self, new_width):
        pass

    # handles
    def get_handles(self):
        # handle_enlarge = ChangeSizeHandle(self)
        handle_move    = MoveHandle(self)              # move the whole item
        handles = [handle_move]
        return handles

    def update_def_from_scene(self):
        """
        Update item definition with scene info
        :return:
        """
        rect = self.rectangle.rect()
        self.item_def[self.center_key] = self.center()
        self.item_def[self.width_key]  = de_scale(rect.width(), self.scale_factor)
        self.item_def[self.height_key] = de_scale(rect.height(), self.scale_factor)
        self.item_def['rotation']      = self.rectangle.rotation()

    def __str__(self):
        self.update_def_from_scene()
        return 'rectangle %s (%s x %s)' % (self.item_def[self.center_key], self.item_def[self.width_key],
                                           self.item_def[self.height_key])


# undo/redo commands
class CopyPasteCommand(QUndoCommand):
    def __init__(self, view, item, pos, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view     = view
        self.item     = item
        self.new_item = None
        self.pos      = pos

    def redo(self):
        self.new_item = self.item.clone()
        self.new_item.translate(self.pos)
        self.view.scene.addItem(self.new_item)

    def undo(self):
        if self.new_item is None:
            return
        self.view.scene.removeItem(self.new_item)


class AddItemCommand(QUndoCommand):
    def __init__(self, view, item, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view = view
        self.item = item

    def redo(self):
        self.view.scene.addItem(self.item)

    def undo(self):
        self.view.scene.removeItem(self.item)


class RemoveItemsCommand(QUndoCommand):
    def __init__(self, view, items, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view  = view
        self.items = items

    def redo(self):
        for item in self.items:
            self.view.remove_item(item)

    def undo(self):
        for item in self.items:
            self.view.scene.addItem(item)


class RemoveItemCommand(QUndoCommand):
    def __init__(self, view, item, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.view = view
        self.item = item

    def redo(self):
        self.view.remove_item(self.item)

    def undo(self):
        self.view.scene.addItem(self.item)


class TranslateCommand(QUndoCommand):
    def __init__(self, item, new_pos, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.item    = item
        self.new_pos = new_pos

    def redo(self):
        self.item.translate(self.new_pos)

    def undo(self):
        self.item.translate(QPointF(-self.new_pos.x(), -self.new_pos.y()))


class ChangeEndPointCommand(QUndoCommand):
    def __init__(self, item, is_start, new_end_point_pos, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.item     = item
        self.is_start = is_start
        self.new_pos  = new_end_point_pos
        self.old_pos  = self.item.p1() if is_start else self.item.p2()

    def redo(self):
        self.item.update_line_end_point(self.is_start, self.new_pos)

    def undo(self):
        self.item.update_line_end_point(self.is_start, self.old_pos)


class ChangeSizeCommand(QUndoCommand):
    def __init__(self, item, new_size, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.item     = item
        self.new_size = new_size

    def redo(self):
        self.item.update_size(self.new_size)

    def undo(self):
        self.item.update_size(-self.new_size)


class ChangePropertiesCommand(QUndoCommand):
    def __init__(self, item, changed_properties, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.item           = item
        self.new_properties = changed_properties
        self.old_properties = self.item.item_def.copy()

    def redo(self):
        self.item.update_properties(self.new_properties)

    def undo(self):
        self.item.update_properties(self.old_properties)


# Handles
class Handle(QGraphicsItemGroup):
    """
    Base class for Handle associated to a parent item. Useful to enlarge, rotate, etc., the parent item
    """
    def __init__(self, parent_item):
        self.parent_item = parent_item

        super().__init__()
        self.setFlag(QGraphicsItem.ItemIsMovable)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges)  # to send itemChange
        # self.setParentItem(self.parent_item)
        self.parent_item.scene().addItem(self)

        self.move_to_parent()

    def move_to_parent(self):
        """
        Position the handle in the scene (depending on where the parent is)
        :return:
        """
        pass

    def update_parent(self, new_position):
        """
        Updates the parent position according to the handle new position
        :param new_position:
        :return:
        """
        pass

    def itemChange(self, change, value):
        """
        Update parent when the handle is moving
        :param change:
        :param value:
        :return:
        """
        if change == QGraphicsItem.ItemPositionChange:
            self.parent_item.remove_handles(non_check_handle=self)   # remove all others items
            self.update_parent(value)
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event):
        """
        Finish the transformation
        :param event:
        :return:
        """
        self.parent_item.end_resizing()
        super().mouseReleaseEvent(event)


class ChangeEndPointHandle(Handle):
    """
    Handle to change one of the end points of a line
    """
    def __init__(self, parent_line, is_start, size=10, arrow_angle_degrees=30, color=(255, 255, 255)):
        self.size         = size
        self.arrow_angle  = arrow_angle_degrees
        self.is_start     = is_start
        self.polygon      = QGraphicsPolygonItem()   # property initialed with move_to_parent
        self.polygon.setBrush(QColor(color[0], color[1], color[2]))

        super().__init__(parent_line)
        self.addToGroup(self.polygon)

    def ordered_end_points(self):
        return [self.parent_item.p2(), self.parent_item.p1()] if self.is_start else \
            [self.parent_item.p1(), self.parent_item.p2()]

    def move_to_parent(self):
        """
        Position the handle in the scene (depending on where the parent is)
        :return:
        """
        pp1, pp2     = self.ordered_end_points()
        polygon      = get_arrow_head(pp1, pp2, self.size, arrow_angle_degrees=self.arrow_angle)
        self.polygon = QGraphicsPolygonItem(polygon)

    def update_parent(self, new_position):
        """
        Updates the parent position according to the handle new position
        :param new_position:
        :return:
        """
        vertex_value  = self.polygon.polygon().last() + new_position
        value_in_line = project_pixel_point_to_segment(self.parent_item.p1(), self.parent_item.p2(),
                                                       vertex_value, in_segment=False)
        command  = ChangeEndPointCommand(self.parent_item, self.is_start, value_in_line)
        self.parent_item.view.add_ui_command(command)


class ChangeSizeHandle(Handle):
    """
    Handle to change the size (like radius) of an Item
    """
    def __init__(self, parent_item, size=(-5, -5, 10, 10),
                 color=(255, 255, 255)):

        self.circle = QGraphicsEllipseItem(size[0], size[1], size[2], size[3])  # property initialed with move_to_parent
        self.circle.setBrush(QColor(color[0], color[1], color[2]))

        super().__init__(parent_item)
        self.addToGroup(self.circle)
        center = self.parent_item.center_pixel_point()
        radius = self.parent_item.radius_pixels()
        pp1    = QPointF(center.x() + radius, center.y())
        self.circle.setPos(pp1)

    def move_to_parent(self):
        pass

    def update_parent(self, new_position):
        """
        Updates the parent position according to the handle new position
        :param new_position: note that new_position is relative to parent
        :return:
        """
        self.circle.setPos(self.circle.pos() + new_position)
        center   = self.parent_item.center_pixel_point()
        new_size = distance_in_pixels(center, self.circle.pos())
        command  = ChangeSizeCommand(self.parent_item, new_size)
        self.parent_item.view.add_ui_command(command)


class MoveHandle(Handle):
    """
    Handle to move an item
    """
    def __init__(self, parent_item, size=10, color=(255, 255, 255)):
        self.size      = size
        self.rectangle = QGraphicsRectItem()  # property initialed with move_to_parent
        self.rectangle.setBrush(QColor(color[0], color[1], color[2]))

        super().__init__(parent_item)
        self.addToGroup(self.rectangle)

    def move_to_parent(self):
        """
        Position the handle in the scene (depending on where the parent is)
        :return:
        """
        pos            = self.parent_item.center_pixel_point()
        rectangle      = QRectF(pos.x()-self.size/2, pos.y()-self.size/2, self.size, self.size)
        self.rectangle = QGraphicsRectItem(rectangle)

    def update_parent(self, new_position):
        """
        Updates the parent position according to the handle new position
        :param new_position:
        :return:
        """
        command = TranslateCommand(self.parent_item, difference_pixel_point(new_position, self.pos()))
        self.parent_item.view.add_ui_command(command)


class RotateHandle(Handle):
    """
    Handle to rotate a line around one of its end points
    """
    def __init__(self, parent_line, is_start, percentage=0.8, size=(-5, -5, 10, 10),
                 color=(255, 255, 255)):
        self.is_start   = is_start
        self.percentage = percentage

        self.circle = QGraphicsEllipseItem(size[0], size[1], size[2], size[3])  # property initialed with move_to_parent
        self.circle.setBrush(QColor(color[0], color[1], color[2]))

        super().__init__(parent_line)
        self.addToGroup(self.circle)

    def move_to_parent(self):
        pp1, pp2 = self.ordered_end_points()
        pp3      = get_point_at_t_pixels(pp1, pp2, t=self.percentage)
        self.circle.setPos(pp3)

    def update_parent(self, new_position):
        """
        Updates the parent position according to the handle new position
        :param new_position: note that new_position is relative to parent
        :return:
        """
        length = self.parent_item.length_in_pixels()
        pp1    = self.non_selected_end_point()
        pp2    = translate_pixel_point(self.circle.pos(), new_position)
        p1, p2 = pixel_points_to_point([pp1, pp2])
        p3     = point_between_points_at_distance(p1, p2, length)
        pp3    = point_to_pixel_point(p3, 1.0)
        self.parent_item.update_line_end_point(self.is_start, pp3)

    def ordered_end_points(self):
        return [self.parent_item.p2(), self.parent_item.p1()] if self.is_start else \
            [self.parent_item.p1(), self.parent_item.p2()]

    def non_selected_end_point(self):
        return self.parent_item.p2() if self.is_start else self.parent_item.p1()


def edit_item_from_ui(item_in_position):
    """
    Edit items properties
    :param item_in_position:
    :return:
    """
    if item_in_position is None:
        return
    item_in_position.edit()


def get_metadata(parent, file_key='metadata_file_name', default_name='editable_items_metadata.yaml'):
    """
    Returns the items' metadata
    :param default_name:
    :param file_key:
    :param parent:
    :return: :type dict
    """
    file_name = getattr(parent, file_key) if hasattr(parent, file_key) else None
    if file_name is None:
        file_name = default_name
    metadata = yf.get_yaml_file(file_name, directory=None)
    return metadata


def get_default_item(item_type, metadata, default_key='default'):
    if default_key not in metadata:
        print('Not default values for type %s' % item_type)
        return {}
    default_def         = metadata[default_key]
    default_def[EditableFigure.type_key] = item_type
    return default_def


def check_must_have_properties(item_def, metadata, req_key='required_properties'):
    for key in metadata[req_key]:
        if key not in item_def:
            return '%s not present in %s, ignored' % (key, item_def)
    return ''  # all required properties are presents


def get_circle(center_point, radius):
    """
    Returns a circle
    :param center_point: :type QPointF
    :param radius: in pixels :type float
    :return:
    """
    x = center_point.x() - radius
    y = center_point.y() - radius
    width  = radius*2
    height = width
    return QGraphicsEllipseItem(x, y, width, height)


def get_rectangle(center: QPointF, width, height, rotation):
    """
    Returns a rectangle centered in center, with the given width and height and rotated rotation
    :param center:
    :param width:
    :param height:
    :param rotation:
    :return:
    """
    rect_item = QGraphicsRectItem(-width / 2, -height / 2, width, height)

    rect_item.setPos(center)

    transform = QTransform()
    transform.translate(center.x(), center.y())
    transform.rotate(rotation)
    transform.translate(-center.x(), -center.y())
    rect_item.setTransform(transform)

    return rect_item


# auxiliary functions
def get_arrow_head(start_point: QPointF, end_point: QPointF, size=10, arrow_angle_degrees=45):
    """
    Returns the head of an arrow aligned with line (start_point, end_point) with the point of the arrow is end_point
    :param arrow_angle_degrees:
    :param start_point: :type QPointF
    :param end_point:   :type QPointF
    :param size: size of each side of the arrow
    :return:
    """
    line_dx     = end_point.x() - start_point.x()
    line_dy     = end_point.y() - start_point.y()
    arrow_angle = math.atan2(line_dy, line_dx)
    angle       = math.radians(arrow_angle_degrees)
    points      = [QPointF(end_point.x() - size * math.cos(arrow_angle + sign * angle),
                           end_point.y() - size * math.sin(arrow_angle + sign * angle)) for sign in [1, -1]]

    points.append(end_point)  # order is important, end_point must be last
    return QPolygonF(points)


def scale(distance, scale_factor):
    """
    Transform a value in meters to a value in pixels
    :param distance: :type float
    :param scale_factor:
    :return: :type int
    """
    return distance * scale_factor


def de_scale(distance, scale_factor):
    """
    Transform a value in pixels to a value in meters
    :param distance: :type int
    :param scale_factor:
    :return: :type float
    """
    return float(distance / scale_factor)


def point_to_pixel_point(point, scale_factor):
    return QPointF(scale(point[0], scale_factor), scale(point[1], scale_factor))


def pixel_point_to_point(q_point, scale_factor, translate):
    return [de_scale(q_point.x() + translate.x(), scale_factor), de_scale(q_point.y() + translate.y(), scale_factor)]


def pixel_points_to_point(pixel_points, scale_factor=1.0, translate=None):
    """
    Convenient form to get point from pixel points at once
    :param pixel_points:
    :param scale_factor:
    :param translate:
    :return:
    """
    translate1 = translate if translate is not None else QPointF(0, 0)
    return [pixel_point_to_point(pp, scale_factor, translate1) for pp in pixel_points]


def distance_in_pixels(p1, p2):
    return math.hypot(p1.x()-p2.x(), p1.y()-p2.y())


def middle_pixel_point(p1, p2):
    return QPointF((p1.x()+p2.x())/2, (p1.y()+p2.y())/2)


def difference_pixel_point(p1, p2):
    return QPointF(p1.x()-p2.x(), p1.y()-p2.y())


def translate_pixel_point(p, translation):
    return QPointF(p.x()+translation.x(), p.y()+translation.y())


def rect_from_points_box(points_box):
    """
    Returns a QRectF from a points box
    x, y = upper left corner
    :param points_box:
    :return:
    """
    min_x, max_x, min_y, max_y = points_box.size()
    return QRectF(min_x, max_y, max_x - min_x, max_y - min_y)


def distance_to_segment(p1: QPointF, p2: QPointF, p3: QPointF):
    """
    given a segment defined by p1, p2, returns distance from p3 to segment
    https://stackoverflow.com/questions/849211/shortest-distance-between-a-point-and-a-line-segment
    answer 3:

    :param p1: start point in segment
    :param p2: end point
    :param p3:
    :return:
    """
    x1, y1 = [p1.x(), p1.y()]
    x2, y2 = [p2.x(), p2.y()]
    x3, y3 = [p3.x(), p3.y()]

    px = x2-x1
    py = y2-y1
    d2 = float(px*px + py*py)

    if abs(d2) < 0.0001:
        # p1 and p2 are too close, just return distance from p1 to p3
        return math.hypot(x1-x3, y1-y3)  # distance_from_points(p1, p3)

    u = ((x3 - x1) * px + (y3 - y1) * py) / d2

    if u < 0.0 or u > 1.0:
        return 999999.0  # any big

    x = x1 + u * px
    y = y1 + u * py

    return math.hypot(x-x3, y-y3)


def project_pixel_point_to_segment(pp1, pp2, pp3, in_segment=False):
    p1, p2, p3 = pixel_points_to_point([pp1, pp2, pp3])
    p4 = project_point_to_segment(p1, p2, p3, in_segment=in_segment)
    return QPointF(p4[0], p4[1])


def get_point_at_t(p1, p2, t):
    """
    Given a t in the parametric equation, returns the corresponding point in the line
    See: http://www.nabla.hr/PC-ParametricEqu1.htm
    :param p1: start point
    :param p2: end point
    :param t:
    :return: [x, y]
    """
    x = p1[0] + (p2[0] - p1[0]) * t
    y = p1[1] + (p2[1] - p1[1]) * t
    return [x, y]


def get_point_at_t_pixels(pp1, pp2, t):
    """
    Same as get_point_at_t but all point are pixel points
    :param pp1:
    :param pp2:
    :param t:
    :return:
    """
    p1, p2 = pixel_points_to_point([pp1, pp2])
    p3     = get_point_at_t(p1, p2, t)
    return point_to_pixel_point(p3, 1.0)


def point_in_line_at_distance(a, point, d, sign):
    """
    Return a new point in line with coefficient a and passing by point, separated d from it
    given there are 2 points that meet the criteria, sign defines which one
    :param a:
    :param point:
    :param d:
    :param sign:
    :return:
    """
    x1, y1 = point
    x2     = x1 + sign*d/math.sqrt(1 + a*a)
    y2     = a*(x2 - x1) + y1
    return x2, y2


def perpendicular_points_from_segment(p1, p2, d):
    """
    Given a segment defined by 2 points, returns the 2 points who are perpendicular to segment in p2 and separated
    d from it
    formula:
    https://math.stackexchange.com/questions/175896/finding-a-point-along-a-line-a-certain-distance-away-from-another-point
    :param p1: start of segment :type [x, y]
    :param p2: end of segment :type [x, y]
    :param d:
    :return: list of points
    """

    (x2, y2) = p2

    if is_horizontal_line(p1, p2):
        return [(x2, y2 + d), (x2, y2 - d)]
    elif is_vertical_line(p1, p2):
        return [(x2 + d, y2), (x2 - d, y2)]

    a, _ = line_slope_equation(p1, p2)
    a90  = perpendicular_slope(a)

    # find target points, in the line before, separated d from p2
    p3s = [point_in_line_at_distance(a90, (x2, y2), d, sign1) for sign1 in [-1, 1]]
    return p3s


def point_between_points_at_distance(p1, p2, d, precision=0.01):
    # returns a point in line with (p1, p2) separated d from p1
    (x1, y1) = p1
    (x2, y2) = p2

    x_sign = relation_sign(x1, x2)

    if similar_values(y1, y2, precision=precision):
        return x1 + x_sign*d, y1
    elif similar_values(x1, x2, precision=precision):
        return x1, y1 + relation_sign(y1, y2)*d

    a  = (y2 - y1)/(x2 - x1)
    p3 = point_in_line_at_distance(a, p1, d, x_sign)
    return p3


def relation_sign(x1, x2):
    if x1 < x2:
        return 1
    elif x1 > x2:
        return -1
    else:
        return 0


def similar_values(l1, l2, precision=0.08):
    return abs(l1 - l2) <= precision


def rectangle_from_line(p1, p2, width):
    """
    Returns a rectangle (its four vertices) whose center line is p1,p2 with width 2*width
    :param p1:
    :param p2:
    :param width:
    :return:
    """
    p21, p22 = perpendicular_points_from_segment(p1, p2, width)
    p11, p12 = perpendicular_points_from_segment(p2, p1, width)
    return p21, p22, p12, p11


def parallel_segments(p1, p2, distance):
    """
    Returns the two segments parallel to segment p1, p2 at distance
    :param p1:
    :param p2:
    :param distance:
    :return:
    """
    p21, p22, p11, p12 = rectangle_from_line(p1, p2, distance)
    return [[p11, p22], [p12, p21]]


def project_point_to_segment(p1, p2, p3, in_segment=True):
    """
    Given a segment defined by p1,p2 returns the projection of p3 on it
    source: https://stackoverflow.com/questions/849211/shortest-distance-between-a-point-and-a-line-segment
            answer 3:
    :param p1:
    :param p2:
    :param p3:
    :param in_segment: whether project only to segment (True) or in the whole line (False)
                       if True then if the projection point is outside the segment returns the closest segment end point
    :return: point :type [x, y]
    """
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    px     = x2-x1
    py     = y2-y1

    d2 = px*px + py*py
    if abs(d2) < 0.0001:
        # segment too short (p1, p2)
        return p1

    u = ((x3 - x1) * px + (y3 - y1) * py) / float(d2)

    if in_segment:
        if u > 1:
            u = 1
        elif u < 0:
            u = 0

    x = x1 + u * px
    y = y1 + u * py

    dx = x - x3
    dy = y - y3

    return x3 + dx, y3 + dy


def perpendicular_slope(slope):
    """
    Returns a slope perpendicular to the one given
    Note: a perpendicular line of a vertical line is horizontal
    :param slope: an angle representing the slope of a line
    :return: :type float
    """
    return -1 / slope if abs(slope) > 0.00001 else float('inf')


def line_slope_equation(p1, p2, near_zero_value=0.0001):
    """
    Returns the parameters of the line slope equation of a line for two given points
    EQ: y = a*x + b
    if line is vertical 'a' is None
    :param near_zero_value: small value used to check for zero with type float
    :param p1:
    :param p2:
    :return: a, b
    """
    diff_xs = p2[0] - p1[0]
    diff_ys = p2[1] - p1[1]
    if abs(diff_xs) < near_zero_value:  # safe way to check 0 with float
        a = None
        if abs(diff_ys) < near_zero_value:
            # both points are the same, no line can be defined
            b = None
        else:
            # it's a vertical line
            b = p2[0]
    else:
        a = float(diff_ys)/diff_xs
        b = p1[1] - a*p1[0]
    return a, b


def is_vertical_line(p1, p2):
    (x1, _) = p1
    (x2, _) = p2
    return near_zero(x1 - x2)


def is_horizontal_line(p1, p2):
    (_, y1) = p1
    (_, y2) = p2
    return near_zero(y1 - y2)


def near_zero(number, precision=0.001):
    return abs(number) < precision
