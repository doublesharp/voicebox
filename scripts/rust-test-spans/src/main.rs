use syn::{
    spanned::Spanned,
    visit::{self, Visit},
    Attribute, ImplItemFn, Item,
};
struct Finder {
    ranges: Vec<(usize, usize)>,
}
fn test_only(attrs: &[Attribute]) -> bool {
    attrs.iter().any(|a| {
        if !a.path().is_ident("cfg") {
            return false;
        }
        let s = match &a.meta {
            syn::Meta::List(list) => list.tokens.to_string(),
            _ => return false,
        };
        let compact: String = s.chars().filter(|c| !c.is_whitespace()).collect();
        compact == "test"
            || (compact.starts_with("all(") && compact[4..].split([',', ')']).any(|x| x == "test"))
    })
}
impl<'a> Visit<'a> for Finder {
    fn visit_item(&mut self, item: &'a Item) {
        let attrs = match item {
            Item::Mod(x) => &x.attrs,
            Item::Fn(x) => &x.attrs,
            Item::Struct(x) => &x.attrs,
            Item::Enum(x) => &x.attrs,
            Item::Impl(x) => &x.attrs,
            Item::Use(x) => &x.attrs,
            Item::Const(x) => &x.attrs,
            Item::Static(x) => &x.attrs,
            Item::Type(x) => &x.attrs,
            Item::Trait(x) => &x.attrs,
            Item::Macro(x) => &x.attrs,
            _ => {
                visit::visit_item(self, item);
                return;
            }
        };
        if test_only(attrs) {
            let span = item.span();
            self.ranges.push((span.start().line, span.end().line));
        } else {
            visit::visit_item(self, item);
        }
    }
    fn visit_impl_item_fn(&mut self, item: &'a ImplItemFn) {
        if test_only(&item.attrs) {
            let span = item.span();
            self.ranges.push((span.start().line, span.end().line));
        } else {
            visit::visit_impl_item_fn(self, item);
        }
    }
}
fn main() {
    for path in std::env::args().skip(1) {
        let source = std::fs::read_to_string(&path).unwrap();
        let file = syn::parse_file(&source).unwrap_or_else(|e| panic!("{}: {}", path, e));
        let mut finder = Finder { ranges: Vec::new() };
        finder.visit_file(&file);
        for (start, end) in finder.ranges {
            println!("{}\t{}\t{}", path, start, end);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifies_test_only_items_without_hiding_production_code() {
        let file = syn::parse_file(
            r#"
fn before() {}
#[cfg(test)]
mod tests { fn helper() {} }
#[cfg(all(test, unix))]
mod platform_tests { fn helper() {} }
#[cfg(not(test))]
fn production() {}
#[cfg(any(test, feature = "debug"))]
fn shared() {}
impl Thing {
    #[cfg(test)]
    fn helper(&self) {}
    fn real(&self) {}
}
"#,
        )
        .unwrap();
        let mut finder = Finder { ranges: Vec::new() };
        finder.visit_file(&file);
        assert_eq!(finder.ranges, [(3, 4), (5, 6), (12, 13)]);
    }
}
